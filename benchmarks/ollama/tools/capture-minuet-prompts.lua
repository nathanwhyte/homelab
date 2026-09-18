-- Capture the FIM prompts the INSTALLED minuet actually produces while typing.
--
-- Every cache claim in IDEA-1105 up to 2026-09-17 was modelled: the probe
-- synthesised a prefix and chose where to put a salt. That decides whether the
-- llama.cpp prefix cache can be reused, which moves idle FIM latency by ~6x --
-- so the model was doing more work than the measurement. This replaces it with
-- the real thing: minuet's own get_context() run against a real buffer while
-- characters are inserted one at a time.
--
-- Why it matters: minuet keeps a fixed-size character window around the cursor
-- (context_window, 1024 here) and, once the buffer exceeds it, keeps the LAST
-- n chars before the cursor (utils.lua strcharpart). The window therefore
-- SLIDES as you type, so consecutive requests can share almost no leading
-- characters even though the file above the cursor never changed.
--
-- Usage (from the repo root):
--   nvim --headless -u NONE \
--     -c "lua CAPTURE_FILE='<source file>' CAPTURE_OUT='/tmp/prompts.jsonl'" \
--     -S benchmarks/ollama/tools/capture-minuet-prompts.lua
--
-- Writes JSONL: {"prefix": ..., "suffix": ..., "common_with_prev": N}
-- `common_with_prev` is the shared leading-character count against the previous
-- prompt -- the cache-eligible portion. Near the full prefix length means the
-- cache helps; near zero means it cannot.

local MINUET = vim.fn.expand('~/.local/share/nvim/lazy/minuet-ai.nvim')
local SRC = CAPTURE_FILE or (MINUET .. '/lua/minuet/utils.lua')
local OUT = CAPTURE_OUT or '/tmp/minuet-prompts.jsonl'
local N = tonumber(CAPTURE_N or '30')
-- Mirrors nvim/lua/plugins/minuet.lua. Kept explicit rather than read from the
-- user config so the capture is reproducible on any machine.
local CONTEXT_WINDOW = tonumber(CAPTURE_WINDOW or '1024')
local CONTEXT_RATIO = tonumber(CAPTURE_RATIO or '0.75')

vim.opt.runtimepath:append(MINUET)

local ok, minuet = pcall(require, 'minuet')
if not ok then
    io.stderr:write('cannot require minuet from ' .. MINUET .. '\n')
    vim.cmd('cquit 1')
end
minuet.config = minuet.config or {}
minuet.config.context_window = CONTEXT_WINDOW
minuet.config.context_ratio = CONTEXT_RATIO

local utils = require('minuet.utils')

vim.cmd('edit ' .. vim.fn.fnameescape(SRC))
local n_lines = vim.api.nvim_buf_line_count(0)
if n_lines < 40 then
    io.stderr:write('source file too short to exercise the context window\n')
    vim.cmd('cquit 1')
end

-- Sit well past the context window so truncation is active, which is the case
-- that matters. Near the top of a file the whole prefix fits and slides never
-- happen.
local line = math.floor(n_lines * 0.7)

local out = assert(io.open(OUT, 'w'))
local prev_prefix = nil
local shared_counts = {}

local function common_prefix_len(a, b)
    if not a or not b then
        return nil
    end
    local n = math.min(#a, #b)
    local i = 1
    while i <= n and a:byte(i) == b:byte(i) do
        i = i + 1
    end
    return i - 1
end

for step = 1, N do
    -- Simulate typing: insert one character at the cursor, exactly as an editor
    -- would, so the buffer (and therefore the window) advances between samples.
    local cur = vim.api.nvim_buf_get_lines(0, line, line + 1, false)[1] or ''
    local typed = cur .. string.char(97 + (step % 26))
    vim.api.nvim_buf_set_lines(0, line, line + 1, false, { typed })

    local cmp_context = {
        cursor = { line = line, col = #typed },
        cursor_before_line = typed,
        cursor_after_line = '',
    }
    local ctx = utils.get_context(cmp_context)
    local shared = common_prefix_len(prev_prefix, ctx.lines_before)
    if shared then
        table.insert(shared_counts, shared)
    end
    out:write(vim.json.encode({
        prefix = ctx.lines_before,
        suffix = ctx.lines_after,
        prefix_chars = #ctx.lines_before,
        common_with_prev = shared,
    }) .. '\n')
    prev_prefix = ctx.lines_before
end
out:close()

local total, mn, mx = 0, nil, nil
for _, v in ipairs(shared_counts) do
    total = total + v
    mn = (mn == nil or v < mn) and v or mn
    mx = (mx == nil or v > mx) and v or mx
end
local mean = #shared_counts > 0 and (total / #shared_counts) or 0
print(
    string.format(
        'wrote %d prompts to %s | shared leading chars vs previous: mean %.1f, min %s, max %s (prefix ~%d chars)',
        N,
        OUT,
        mean,
        tostring(mn),
        tostring(mx),
        prev_prefix and #prev_prefix or 0
    )
)
vim.cmd('qall!')
