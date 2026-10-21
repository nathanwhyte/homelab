#!/usr/bin/env python3
"""Tests for check-secret-files.py, the pre-commit guard for this public repo.

Two classes, which the hooks in .pre-commit-config.yaml run separately:

  CheckerCaseTests  calls the checker on one file per case and compares the
                    result with the case's `should_fail`. At least one case per
                    Codex review finding on homelab#242, so reverting a fix
                    fails a case.
  DispatchTests     stages fixtures in a scratch Git repo and runs the real
                    `gitleaks` and `secret-files` hooks through `pre-commit
                    run`, since a call to the script skips everything
                    pre-commit decides first (which files reach the hook, which
                    flags gitleaks gets, what it reads from the repo root).

Pass a class name to run just that one. Every value is synthetic, and
key-shaped ones are built at runtime so this file passes the gitleaks hook it
tests. Nothing here reads a credential. DispatchTests skips (exit 0, SKIP
printed) when pre-commit or its hook environments are unavailable offline; set
CHECK_SECRET_FILES_REQUIRE_DISPATCH=1 to fail instead.
"""

import base64
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CHECKER = HERE / "check-secret-files.py"

spec = importlib.util.spec_from_file_location("check_secret_files", CHECKER)
csf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(csf)

SECRET = "apiVersion: v1\nkind: Secret\nmetadata: {{name: s}}\nstringData:\n{body}\n"


def secret(body: str) -> str:
    return SECRET.format(body=body)


# Built, not written out: a base64 literal next to a key-like name trips gitleaks.
B64_ANGLE = base64.b64encode(b"<R2_SECRET_ACCESS_KEY>").decode()


def annotated_list() -> str:
    """100 ConfigMaps with last-applied annotations, then a Secret that hides a value."""
    items = "".join(
        f"- kind: ConfigMap\n  metadata:\n    name: c{i}\n    annotations:\n"
        f'      kubectl.kubernetes.io/last-applied-configuration: \'{{"kind":"ConfigMap"}}\'\n'
        for i in range(100)
    )
    items += (
        "- kind: Secret\n  metadata:\n    name: s\n    annotations:\n"
        "      kubectl.kubernetes.io/last-applied-configuration: "
        '\'{"kind":"Secret","stringData":{"p":"hunter2x"}}\'\n'
        "  stringData: {p: REPLACE_ME}\n"
    )
    return "kind: List\nitems:\n" + items


def billion_laughs() -> str:
    levels = "".join(
        f"{chr(98 + i)}: &{chr(98 + i)} [{','.join(['*' + chr(97 + i)] * 10)}]\n"
        for i in range(7)
    )
    return "a: &a [1,1,1,1,1,1,1,1,1,1]\n" + levels


# label -> (path relative to the repo root, content, should_fail). Content is
# text, or bytes for the encodings the checker must refuse.
CASES = {
    "placeholder substring": ("a.yaml", secret("  auth: N7!examplePassword9"), True),
    "placeholder substring 2": ("a.yaml", secret("  password: dK7exampleQ9!2pX"), True),
    "replace substring": ("a.yaml", secret("  password: myreplacepw9"), True),
    "uppercase word": ("a.yaml", secret("  password: HUNTERTWO"), True),
    "_PATH suffix": ("a.yaml", secret("  DB_PATH: s3cure9q"), True),
    "_USER suffix": ("a.yaml", secret("  PASSWORD_FOR_USER: s3cure9!q"), True),
    "password_path": (
        "a.yaml",
        secret("  DATABASE_PASSWORD_PATH: dK7bF9!2pX"),
        True,
    ),
    "nested under data": (
        "a.yaml",
        "data:\n  nested:\n    kind: Secret\n    metadata: {name: n}\n"
        "    stringData: {password: s3cure9q}\n",
        True,
    ),
    "later yaml tag": (
        "a.yaml",
        secret("  password: s3cure9q") + "---\ncustom: !Ref parameter\n",
        True,
    ),
    "unparseable secret": (
        "a.yaml",
        "kind: Secret\nstringData:\n  password: [unclosed\n",
        True,
    ),
    "url with real password": (
        "a.yaml",
        secret("  DB_URL: postgres://u:s3cure9q@db:5432/x"),
        True,
    ),
    "secret dir yaml": ("secrets/db.yaml", "a: 1\n", True),
    "json metadata string": (
        "a.json",
        '{"metadata":"dataset description","rows":[1,2]}',
        False,
    ),
    "json metadata 2": (
        "a.json",
        '{"metadata":"catalogue","password":"read this documentation","token":123}',
        False,
    ),
    "secretKeyRef deployment": (
        "d.yaml",
        "kind: Deployment\nspec:\n  env:\n"
        "  - valueFrom: {secretKeyRef: {name: s, key: k}}\n",
        False,
    ),
    "placeholder example": (
        "x.secret.yaml.example",
        secret(
            "  A: REPLACE_ME\n  B: <token>\n  C: ${VAR}\n  D: REPLACE_WITH_KEY\n"
            "  E: ROOT_TOKEN_PLACEHOLDER"
        ),
        True,
    ),
    "config keys": (
        "x.secret.yaml.example",
        secret(
            '  RCLONE_CONFIG_R2_ENDPOINT: "https://abc.r2.cloudflarestorage.com"\n'
            "  RCLONE_CONFIG_R2_REGION: auto\n"
            '  RCLONE_CONFIG_R2_NO_CHECK_BUCKET: "true"\n  USER: noot'
        ),
        True,
    ),
    "url placeholder password": (
        "x.secret.yaml.example",
        secret("  DB_URL: postgresql+asyncpg://u:CHANGE_ME@db:5432/x"),
        True,
    ),
    "helm template no secret": (
        "t.yaml",
        "kind: ConfigMap\ndata:\n  a: {{ .Values.a }}\n",
        True,
    ),
    "code in secret dir": ("secrets/rotate.py", "print(1)\n", True),
    "example in credential dir": (
        "credentials/c.secret.yaml.example",
        secret("  A: REPLACE_ME"),
        True,
    ),
    "cfn tags": ("cfn.yaml", "Resources:\n  B: !Ref Thing\n", True),
    # round 4
    "escaped kind + dup json": (
        "e.json",
        '{"kind":"\\u0053ecret","stringData":{"password":"probe-value"},"x":1,"x":2}',
        True,
    ),
    "secretlist item": (
        "sl.yaml",
        "kind: SecretList\nitems:\n- metadata: {name: a}\n"
        "  data: {password: aHVudGVyMng=}\n",
        True,
    ),
    "dotenv example": (
        ".env.prod.example",
        "# Secret configuration\nPASSWORD=REPLACE_ME\n"
        "FEATURE_FLAGS='{\"enabled\": true}'\n",
        False,
    ),
    "bad int tag": (
        "i.yaml",
        secret("  password: !!int PROBEPRIVATEVALUE123"),
        True,
    ),
    "b64 angle placeholder": (
        "b2.yaml.example",
        f"kind: Secret\ndata:\n  password: {B64_ANGLE}\n",
        True,
    ),
    "raw angle in data": (
        "b3.yaml.example",
        "kind: Secret\ndata:\n  password: <R2_SECRET_ACCESS_KEY>\n",
        False,
    ),
    "code under secret dir": ("secrets.py/x.sh", "echo\n", True),
    "json under example dir": ("credentials.example/prod.json", "{}", True),
    # fail-closed redesign
    "config value in secret": (
        "a.yaml",
        secret("  RCLONE_CONFIG_R2_REGION: auto"),
        True,
    ),
    "placeholder-pw url": (
        "a.yaml",
        secret("  DB_URL: postgres://u:CHANGE_ME@db/x"),
        True,
    ),
    "lowercase angle": ("a.yaml", secret("  A: <token>"), True),
    "exact placeholders": (
        "x.yaml.example",
        secret(
            '  A: REPLACE_ME\n  B: CHANGE_ME\n  C: "<R2_ENDPOINT>" # https://x\n  D: ""'
        ),
        False,
    ),
    "b64 block placeholder": (
        "b.yaml.example",
        "kind: Secret\ndata:\n  password: |\n    UkVQTEFDRV9NRQ==\n",
        True,
    ),
    # round 5
    "alias into secretlist": (
        "v1.yaml",
        "defaults: &x {data: {password: bGV0bWVpbg==}}\n"
        "objects: {kind: SecretList, items: [*x]}\n",
        True,
    ),
    "alias template secretlist": (
        "v2.yaml",
        "template: &t\n  stringData: {password: hunter2}\n"
        "manifest:\n  kind: SecretList\n  items: [*t]\n",
        True,
    ),
    "alias secret twice": (
        "v3.yaml",
        "a: &s {kind: Secret, stringData: {p: REPLACE_ME}}\nb: [*s, *s]\n",
        True,
    ),
    "whitespace value": ("w.yaml", secret('  password: "  "'), True),
    "padded placeholder": ("w2.yaml", secret('  password: " REPLACE_ME"'), True),
    "b64 inline placeholder": (
        "b4.yaml",
        "kind: Secret\ndata:\n  password: UkVQTEFDRV9NRQ==\n",
        True,
    ),
    "tagged dup key": (
        "t.yaml",
        "kind: Secret\nstringData:\n  password: hunter2\n  !custom password: REPLACE_ME\n",
        True,
    ),
    "int/str key ok": ("k.yaml", "a:\n  1: x\n  '1': y\n", False),
    # round 6
    "recursive alias nested": (
        "r1.yaml",
        "object: &a\n  stringData: {password: probe-pw}\n"
        "  nested: {kind: SecretList, items: [*a]}\n",
        True,
    ),
    "recursive alias root": (
        "r2.yaml",
        "&root\nmetadata: {name: cycle}\nstringData: {password: probe-pw}\n"
        "child:\n  kind: SecretList\n  items: [*root]\n",
        True,
    ),
    "omap secret": (
        "o1.yaml",
        "wrapper: !!omap\n  - x:\n      kind: Secret\n"
        "      stringData: {password: probe-pw}\n",
        True,
    ),
    "pairs secret": (
        "o2.yaml",
        "wrapper: !!pairs\n  - x:\n      kind: Secret\n"
        "      stringData: {password: probe-pw}\n",
        True,
    ),
    "binary kind": (
        "bk.yaml",
        "kind: !!binary U2VjcmV0\nstringData: {password: probe-pw}\n",
        True,
    ),
    "merge override": (
        "mo.yaml",
        "kind: Secret\nstringData:\n  <<: {password: probe-pw}\n  password: REPLACE_ME\n",
        True,
    ),
    "nested merge reuse ok": (
        "nm.yaml",
        "copy:\n  <<: &defaults\n    <<: {a: x}\n    a: y\ndefaults: *defaults\n",
        False,
    ),
    "anchors in configmap ok": (
        "cm.yaml",
        "kind: ConfigMap\nx: &v {a: 1}\ny: *v\n",
        False,
    ),
    "hex/int dup key": (
        "hx.yaml",
        "kind: Secret\nstringData:\n  0x1: probe-pw\n  1: REPLACE_ME\n",
        True,
    ),
    # round 7
    "null-tagged kind": (
        "n1.yaml",
        "kind: !!null Secret\nstringData: {password: hunter2}\n",
        True,
    ),
    "null-tagged stringData": (
        "n2.yaml",
        "kind: Secret\nstringData: !!null hunter2\n",
        True,
    ),
    "str-tagged value": (
        "n3.yaml",
        "kind: Secret\nstringData: {password: !!str hunter2}\n",
        True,
    ),
    "escaped last-applied in merge": (
        "la.yaml",
        "kind: ConfigMap\nmetadata:\n  annotations:\n"
        "    <<: {kubectl.kubernetes.io/last-applied-configuration: "
        '\'{"kind":"Secr\\u0065t","stringData":{"password":"probe"}}\'}\n'
        '    kubectl.kubernetes.io/last-applied-configuration: \'{"kind":"ConfigMap"}\'\n',
        True,
    ),
    "deployment imagePullSecrets + anchors ok": (
        "dp.yaml",
        "kind: Deployment\nspec:\n  template:\n    spec:\n"
        "      imagePullSecrets: [{name: registry}]\n      containers:\n"
        "      - {name: a, env: &env [{name: X, value: '1'}]}\n"
        "      - {name: b, env: *env}\n",
        False,
    ),
    "helm existingSecret + anchors ok": (
        "hv.yaml",
        "auth: {existingSecret: db-auth}\nres: &r {cpu: 1}\nother: *r\n",
        False,
    ),
    "control char": ("cc.yaml", 'a: "x\x01"\n', True),
    "billion laughs": ("bl.yaml", billion_laughs(), True),
    "utf-32 bom": (
        "u32.yaml",
        secret("  password: hunter2x").encode("utf-32"),
        True,
    ),
    "utf-16be no bom": (
        "u16be.yaml",
        secret("  password: hunter2x").encode("utf-16-be"),
        True,
    ),
    "100 annotated then secret": ("big.yaml", annotated_list(), True),
    "helm secret template": (
        "h3.yaml",
        "kind: Secret\nstringData:\n  password: {{ .Values.pw }}\n",
        True,
    ),
    # round 2
    "config url query password": (
        "a.yaml",
        secret("  endpoint: https://db.internal/?password=hunter2x"),
        True,
    ),
    "config url userinfo": (
        "a.yaml",
        secret("  endpoint: https://tok9x@db.internal/"),
        True,
    ),
    "placeholder url + query": (
        "a.yaml",
        secret("  DB_URL: postgres://app:CHANGE_ME@db.internal/app?password=hunter2"),
        True,
    ),
    "placeholder url + token user": (
        "a.yaml",
        secret("  DB_URL: postgres://tok9x:CHANGE_ME@db/app"),
        True,
    ),
    "dup json keys": (
        "a.json",
        '{"kind":"Secret","stringData":{"password":"hunter2x"},'
        '"stringData":{"password":"REPLACE_ME"}}',
        True,
    ),
    "dup inner keys": (
        "a.json",
        '{"kind":"Secret","stringData":{"password":"hunter2x","password":"REPLACE_ME"}}',
        True,
    ),
    "dup kind": (
        "a.yaml",
        "kind: Secret\nstringData: {password: hunter2x}\nkind: ConfigMap\n",
        True,
    ),
    "dup in last-applied": (
        "a.yaml",
        "kind: Secret\nmetadata:\n  name: s\n  annotations:\n"
        "    kubectl.kubernetes.io/last-applied-configuration: "
        '\'{"kind":"Secret","stringData":{"p":"hunter2x"},'
        '"stringData":{"p":"REPLACE_ME"}}\'\n',
        True,
    ),
    "uppercase suffix": ("m.YAML", secret("  password: hunter2x"), True),
    "utf-16 secret": (
        "u16.yaml",
        secret("  password: hunter2x").encode("utf-16"),
        True,
    ),
    "helm example template": (
        "h.yaml.example",
        "kind: Secret\nmetadata:\n  name: {{ .Release.Name }}-auth\n"
        "{{- if .Values.x }}\nstringData:\n  password: REPLACE_ME\n{{- end }}\n",
        True,
    ),
    "helm literal default": (
        "h2.yaml",
        'kind: Secret\nstringData:\n  password: {{ .Values.pw | default "hunter2x" }}\n',
        True,
    ),
    "alias cycle": ("c.yaml", "defaults: &defaults {parent: *defaults}\n", False),
    "merge keys ok": ("m.yaml", "base: &b {a: 1}\nx:\n  <<: *b\n  a: 2\n", False),
}


class CheckerCaseTests(unittest.TestCase):
    def test_case_count(self):
        # Dropping a case drops a regression guard; raise this with a new one.
        self.assertEqual(len(CASES), 78)

    def test_names_and_symlinks(self):
        # Cases above write regular files under non-secret names; these cover
        # the name rule and the symlink rule.
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                for name in ("db.secret.yaml", "tls.key", ".env", "kubeconfig"):
                    Path(name).write_text("a: 1\n")
                    self.assertTrue(csf.check(Path(name)), f"{name} should fail")
                Path("ok.yaml").write_text("a: 1\n")
                Path("link.yaml").symlink_to("ok.yaml")
                Path("db.secret.yaml.example").write_text("a: 1\n")
                self.assertTrue(csf.check(Path("link.yaml")), "symlink should fail")
                self.assertFalse(csf.check(Path("ok.yaml")))
                self.assertFalse(csf.check(Path("db.secret.yaml.example")))
            finally:
                os.chdir(cwd)

    def test_cases(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                for label, (name, content, should_fail) in CASES.items():
                    with self.subTest(label):
                        path = Path(name)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        if isinstance(content, bytes):
                            path.write_bytes(content)
                        else:
                            path.write_text(content)
                        # The checker reads path parts, so pass the repo-relative
                        # form, as pre-commit does.
                        problems = csf.check(path)
                        self.assertEqual(
                            bool(problems),
                            should_fail,
                            f"{label}: expected {'a finding' if should_fail else 'none'},"
                            f" got {len(problems)}",
                        )
            finally:
                os.chdir(cwd)


# --- Real pre-commit dispatch -------------------------------------------------

HOOKS = ("gitleaks", "secret-files")
# Files the scratch repo needs beside the checker: gitleaks reads both.
GITLEAKS_FILES = (".gitleaks.toml", ".gitleaks-report.tmpl")
# Synthetic S3-style key pair, built here so no key-shaped literal sits in this file.
HEX = "0123456789abcdef"
KEY_PAIR = f"AK={HEX * 2}\nSK={HEX * 4}\n"
PLACEHOLDER_SECRET = "kind: Secret\ndata:\n  password: REPLACE_ME\n"
B64_PLACEHOLDER_SECRET = f"kind: Secret\ndata:\n  password: {B64_ANGLE}\n"
SHORT_B64_PLACEHOLDER_SECRET = "kind: Secret\ndata:\n  password: UkVQTEFDRV9NRQ==\n"


class Link(str):
    """A fixture that is a symlink to this target."""


def clean_env() -> dict[str, str]:
    # pre-commit sets GIT_* when it runs a hook, and a test run from one would
    # leak them into the scratch repo's git (IMPR-1266).
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["PRE_COMMIT_COLOR"] = "never"
    return env


def restricted_config() -> str:
    """The head's config with only the gitleaks and secret-files hooks."""
    config = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text())
    repos = []
    for repo in config["repos"]:
        hooks = [h for h in repo["hooks"] if h["id"] in HOOKS]
        if hooks:
            repos.append({**repo, "hooks": hooks})
    kept = {h["id"] for r in repos for h in r["hooks"]}
    assert kept == set(HOOKS), f"config lost a hook: {kept}"
    return yaml.safe_dump({**config, "repos": repos}, sort_keys=False)


class DispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.env = clean_env()
        try:
            cls.build_scratch_repo()
        except unittest.SkipTest as skip:
            if os.environ.get("CHECK_SECRET_FILES_REQUIRE_DISPATCH") == "1":
                raise AssertionError(f"dispatch tests required: {skip}") from None
            print(f"SKIP DispatchTests: {skip}")
            raise

    @classmethod
    def run_in_repo(cls, cmd: list[str], timeout: int = 600):
        return subprocess.run(
            cmd,
            cwd=cls.root,
            env=cls.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    @classmethod
    def git(cls, *args: str) -> str:
        done = cls.run_in_repo(["git", *args])
        assert done.returncode == 0, f"git {args}: {done.stderr}"
        return done.stdout

    @classmethod
    def build_scratch_repo(cls):
        if shutil.which("pre-commit") is None:
            raise unittest.SkipTest("pre-commit is not installed")
        (cls.root / "_scripts").mkdir()
        shutil.copy2(CHECKER, cls.root / "_scripts" / CHECKER.name)
        for name in GITLEAKS_FILES:
            shutil.copy2(REPO / name, cls.root / name)
        (cls.root / ".pre-commit-config.yaml").write_text(restricted_config())
        cls.git("init", "-q")
        cls.git("config", "user.name", "scratch")
        cls.git("config", "user.email", "scratch@example.invalid")
        cls.git("config", "commit.gpgsign", "false")
        cls.git("add", "-f", ".")
        cls.git("commit", "-q", "-m", "scratch")
        # Hook environments come from pre-commit's cache; a first run offline
        # cannot build them, so skip rather than fail for lack of network.
        try:
            done = cls.run_in_repo(["pre-commit", "install-hooks"], timeout=300)
        except subprocess.TimeoutExpired:
            raise unittest.SkipTest("pre-commit install-hooks timed out") from None
        if done.returncode:
            tail = (done.stdout + done.stderr).strip().splitlines()[-3:]
            raise unittest.SkipTest(
                "pre-commit cannot install the gitleaks and secret-files hook"
                f" environments (offline, or no go toolchain): {' | '.join(tail)}"
            )

    def stage(self, files: dict[str, str | Link]):
        self.addCleanup(self.reset)
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, Link):
                path.symlink_to(content)
            else:
                path.write_text(content)
        self.git("add", "-f", "-A")

    def reset(self):
        self.git("reset", "-q", "--hard", "HEAD")
        self.git("clean", "-fdq")

    def hook_passes(self, hook: str, files: list[str]) -> bool:
        done = self.run_in_repo(["pre-commit", "run", hook, "--files", *files])
        out = done.stdout + done.stderr
        # A skipped hook exits 0, which must not read as a pass.
        self.assertNotIn("(no files to check)", out, f"{hook} skipped its files")
        if done.returncode == 0:
            self.assertIn("Passed", out)
            return True
        self.assertIn("- exit code:", out, f"{hook} errored rather than failed:\n{out}")
        return False

    def expect(self, files: dict[str, str | Link], **verdicts: bool):
        """Stage `files`, run each hook, and compare with `hook=passes`."""
        self.stage(files)
        for hook, passes in verdicts.items():
            hook_id = hook.replace("_", "-")
            self.assertEqual(
                self.hook_passes(hook_id, list(files)),
                passes,
                f"{hook_id} should {'pass' if passes else 'fail'} on {list(files)}",
            )

    def test_clean_file_passes_both(self):
        self.expect(
            {"ok.yaml": "kind: ConfigMap\ndata: {a: b}\n"},
            gitleaks=True,
            secret_files=True,
        )

    def test_placeholder_secret_passes_both(self):
        self.expect(
            {"db.yaml.example": PLACEHOLDER_SECRET}, gitleaks=True, secret_files=True
        )

    def test_symlink_reaches_the_hook(self):
        # `types: [file]` (the default) makes pre-commit skip symlinks, so the
        # script is never called; the config clears `types` for this.
        # gitleaks skips a symlink-only change, so only secret-files is judged.
        self.expect({"link.yaml": Link("ok.yaml")}, secret_files=False)

    def test_key_pair_fails_gitleaks(self):
        self.expect({"keys.txt": KEY_PAIR}, gitleaks=False, secret_files=True)

    def test_gitleaks_allow_comment_does_not_wave_a_key_pair_through(self):
        self.expect(
            {"keys.txt": f"# gitleaks:allow\n{KEY_PAIR}"},
            gitleaks=False,
            secret_files=True,
        )

    def test_inline_gitleaks_allow_does_not_wave_a_key_pair_through(self):
        allowed = "".join(
            f"{line} # gitleaks:allow\n" for line in KEY_PAIR.splitlines()
        )
        self.expect({"keys.txt": allowed}, gitleaks=False, secret_files=True)

    def test_base64_placeholder_fails_both(self):
        # gitleaks flags a base64 `<R2_SECRET_ACCESS_KEY>` placeholder, so
        # encoding one does not get it past the hooks; the checker wants the
        # plain placeholder in any case.
        self.expect(
            {"b64.yaml.example": B64_PLACEHOLDER_SECRET},
            gitleaks=False,
            secret_files=False,
        )

    def test_short_base64_placeholder_fails_the_checker(self):
        # Too short for gitleaks to flag, so only the checker stops it.
        self.expect(
            {"b64.yaml.example": SHORT_B64_PLACEHOLDER_SECRET},
            gitleaks=True,
            secret_files=False,
        )

    def test_gitleaksignore_fails_secret_files(self):
        # gitleaks honours .gitleaksignore even with --ignore-gitleaks-allow, so
        # the checker refuses the file itself.
        self.expect(
            {".gitleaksignore": "keys.txt:s3-style-key-pair:1\n", "ok.yaml": "a: 1\n"},
            secret_files=False,
        )

    def test_gitleaksignore_symlink_fails_secret_files(self):
        self.expect(
            {".gitleaksignore": Link("elsewhere"), "ok.yaml": "a: 1\n"},
            secret_files=False,
        )

    def test_force_added_secret_file_fails_secret_files(self):
        self.expect({"db.secret.yaml": "a: 1\n"}, secret_files=False)


if __name__ == "__main__":
    unittest.main()
