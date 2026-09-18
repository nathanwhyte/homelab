-- Compare the SUGGESTIONS two FIM tags produce, through the real minuet stack.
--
-- The contention probe sends its own /v1/completions request and reads the raw
-- text back. That is not what an editor shows you. minuet:
--   * builds the request from provider_options (here: max_tokens 64,
--     top_p 0.9 -- sampled, NOT the temperature 0 the probe uses), and
--   * post-processes the response through prepare_fim_items(), which runs
--     filter_context_sequences_in_items() and drops anything that merely echoes
--     surrounding context.
-- So the probe can score a "completion" that minuet discards as empty. This
-- script calls minuet's own backend and records what actually survives.
--
-- Usage (from the homelab repo root):
--   MINUET_FIM_MODEL=deepseek-coder-v2:fim \
--   nvim --headless -u NONE \
--     -c "lua CMP_FILE='<source file>' CMP_OUT='/tmp/sugg-fim.jsonl'" \
--     -S benchmarks/ollama/tools/compare-fim-suggestions.lua
--
-- Then run again with the other tag and diff the two files. Because sampling is
-- on, identical prompts can still differ between runs -- treat single-position
-- differences as noise and read the aggregate (empty rate, length, shape).

local MINUET = vim.fn.expand('~/.local/share/nvim/lazy/minuet-ai.nvim')
local PLENARY = vim.fn.expand('~/.local/share/nvim/lazy/plenary.nvim')
local SPEC = vim.fn.expand('~/code/dotfiles/nvim/lua/plugins/minuet.lua')
local SRC = CMP_FILE or (MINUET .. '/lua/minuet/utils.lua')
local OUT = CMP_OUT or '/tmp/minuet-suggestions.jsonl'
local N = tonumber(CMP_N or '20')
local TIMEOUT_MS = tonumber(CMP_TIMEOUT or '20000')

vim.opt.runtimepath:append(MINUET)
vim.opt.runtimepath:append(PLENARY)

-- Load the REAL user config rather than restating it, so this measures what is
-- deployed. MINUET_FIM_MODEL is honoured by the spec itself.
local spec = dofile(SPEC)
local opts = spec[1] and spec[1].opts
if not opts then
    io.stderr:write('could not read opts from ' .. SPEC .. '\n')
    vim.cmd('cquit 1')
end

local minuet = require('minuet')
minuet.setup(opts)
local model = minuet.config.provider_options.openai_fim_compatible.model

local utils = require('minuet.utils')
local backend = require('minuet.backends.openai_fim_compatible')

vim.cmd('edit ' .. vim.fn.fnameescape(SRC))
local n_lines = vim.api.nvim_buf_line_count(0)

local out = assert(io.open(OUT, 'w'))
local n_empty, n_ok, lens = 0, 0, {}

-- Spread positions through the second half of the file so the context window is
-- saturated at every sample (near the top the whole prefix fits and the case is
-- unrepresentative).
for i = 1, N do
    local line = math.floor(n_lines * 0.5) + math.floor((n_lines * 0.45) * (i - 1) / math.max(1, N - 1))
    local text = vim.api.nvim_buf_get_lines(0, line, line + 1, false)[1] or ''
    -- Cut the line at ~60% to create a genuine fill-in-the-middle hole rather
    -- than an end-of-line continuation.
    -- CMP_CUT_RATIO 1.0 puts the cursor at end-of-line (nothing after it), which
    -- is the common editing case. A mid-line cut leaves after-text that a
    -- CORRECT completion would reproduce -- and minuet's
    -- filter_context_sequences_in_items() then discards it as an echo, which
    -- inflates the empty rate. Check both before reading anything into it.
    local cut_ratio = tonumber(CMP_CUT_RATIO or '0.6')
    local cut = math.max(0, math.floor(#text * cut_ratio))
    local before, after = text:sub(1, cut), text:sub(cut + 1)

    local cmp_context = {
        cursor = { line = line, col = cut },
        cursor_before_line = before,
        cursor_after_line = after,
    }
    local context = utils.get_context(cmp_context)

    local done, items = false, nil
    local t0 = vim.uv.hrtime()
    backend.complete(context, function(result)
        items = result
        done = true
    end)
    vim.wait(TIMEOUT_MS, function()
        return done
    end, 50)
    local ms = (vim.uv.hrtime() - t0) / 1e6

    local first = items and items[1] or nil
    if first then
        n_ok = n_ok + 1
        table.insert(lens, #first)
    else
        n_empty = n_empty + 1
    end
    out:write(vim.json.encode({
        model = model,
        line = line,
        timed_out = not done,
        n_items = items and #items or 0,
        suggestion = first,
        suggestion_chars = first and #first or 0,
        ms = ms,
        prefix_chars = #context.lines_before,
    }) .. '\n')
end
out:close()

table.sort(lens)
local median = #lens > 0 and lens[math.ceil(#lens / 2)] or 0
print(
    string.format(
        '%s -> %s | %d suggestions, %d EMPTY after minuet filtering, median %d chars',
        model,
        OUT,
        n_ok,
        n_empty,
        median
    )
)
vim.cmd('qall!')
