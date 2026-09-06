import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

LABEL_RE = re.compile(r"[^a-zA-Z0-9_:]")


def label_value(value):
    value = str(value or "unknown")
    return value.replace("\\", r"\\").replace('"', r"\"").replace("\n", " ")


def dsn():
    return (
        f"host={os.environ.get('POSTGRES_HOST', 'mem0-postgres.mem0.svc.cluster.local')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"dbname={os.environ.get('POSTGRES_DB', 'postgres')} "
        f"user={os.environ.get('POSTGRES_USER', 'postgres')} "
        f"password={os.environ['POSTGRES_PASSWORD']} "
        "connect_timeout=5"
    )


def query_metrics():
    lines = [
        "# HELP mem0_exporter_up Whether the mem0 exporter can query Postgres",
        "# TYPE mem0_exporter_up gauge",
    ]
    try:
        with psycopg.connect(dsn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select
                      count(*)::bigint as total,
                      extract(epoch from min((payload->>'created_at')::timestamptz))::bigint as oldest,
                      extract(epoch from max((payload->>'created_at')::timestamptz))::bigint as newest
                    from public.memories
                    """
                )
                total, oldest, newest = cur.fetchone()
                lines += [
                    "mem0_exporter_up 1",
                    "# HELP mem0_memories_total Total stored mem0 memories",
                    "# TYPE mem0_memories_total gauge",
                    f"mem0_memories_total {total or 0}",
                    "# HELP mem0_memory_oldest_timestamp_seconds Oldest memory created_at Unix timestamp",
                    "# TYPE mem0_memory_oldest_timestamp_seconds gauge",
                    f"mem0_memory_oldest_timestamp_seconds {oldest or 0}",
                    "# HELP mem0_memory_newest_timestamp_seconds Newest memory created_at Unix timestamp",
                    "# TYPE mem0_memory_newest_timestamp_seconds gauge",
                    f"mem0_memory_newest_timestamp_seconds {newest or 0}",
                ]

                groups = [
                    (
                        "mem0_memories_by_user",
                        "user_id",
                        "coalesce(payload->>'user_id','unknown')",
                    ),
                    (
                        "mem0_memories_by_agent",
                        "agent_id",
                        "coalesce(payload->>'agent_id','unknown')",
                    ),
                    (
                        "mem0_memories_by_role",
                        "role",
                        "coalesce(payload->>'role','unknown')",
                    ),
                ]
                for metric, label, expr in groups:
                    lines += [
                        f"# HELP {metric} Stored mem0 memories grouped by {label}",
                        f"# TYPE {metric} gauge",
                    ]
                    cur.execute(
                        f"select {expr} as label, count(*)::bigint from public.memories group by 1 order by 2 desc, 1"
                    )
                    for value, count in cur.fetchall():
                        lines.append(
                            f'{metric}{{{label}="{label_value(value)}"}} {count}'
                        )

                lines += [
                    "# HELP mem0_memory_metadata_key_total Stored mem0 memories containing each metadata key",
                    "# TYPE mem0_memory_metadata_key_total gauge",
                ]
                cur.execute(
                    """
                    select key, count(*)::bigint
                    from public.memories,
                         jsonb_object_keys(coalesce(payload->'metadata','{}'::jsonb)) as key
                    group by key
                    order by 2 desc, key
                    """
                )
                for key, count in cur.fetchall():
                    lines.append(
                        f'mem0_memory_metadata_key_total{{key="{label_value(key)}"}} {count}'
                    )
    except Exception as exc:
        lines += [
            "mem0_exporter_up 0",
            f"# scrape_error {label_value(type(exc).__name__ + ': ' + str(exc))}",
        ]
    return ("\n".join(lines) + "\n").encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok\n")
            return
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = query_metrics()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "9090"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
