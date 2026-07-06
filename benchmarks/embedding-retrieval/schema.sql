-- Scratch pgvector schema for the TASK-1122 embedder A/B (Phase 0 item 3).
-- Mirrors omnipendium's entry_embeddings shape (id + doc metadata + vector),
-- but one table PER (model, dimension) so candidates with different dims
-- coexist. bge-m3 adds a sparse sidecar column for the hybrid arm.
--
-- Tables are created per-candidate by the harness (dimension varies), so this
-- file only installs the extension and documents the canonical shape. The
-- harness runs the CREATE TABLE with the right Vector(n) for each model.

CREATE EXTENSION IF NOT EXISTS vector;

-- Canonical per-candidate table shape (harness substitutes <name>/<dim>):
--
--   CREATE TABLE emb_<name> (
--       entry_id     text PRIMARY KEY,   -- e.g. 'info-1021' (the match_fragment)
--       vault_path   text NOT NULL,
--       doc_text     text NOT NULL,      -- exactly what was embedded (post-prefix)
--       embedding    vector(<dim>) NOT NULL,
--       sparse       jsonb               -- bge-m3 hybrid only; NULL otherwise
--   );
--   CREATE INDEX ON emb_<name> USING hnsw (embedding vector_cosine_ops);
--
-- Query ranking: ORDER BY embedding <=> :query_vec  (cosine distance).
-- Hybrid (bge-m3): blend dense cosine with sparse dot-product in the harness.
