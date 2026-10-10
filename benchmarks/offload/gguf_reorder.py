"""Merge a split GGUF into one file with GPU-side tensors first and experts last.

llama.cpp on Apple Silicon maps the mmapped file range that holds GPU-assigned tensors into
Metal. In the original layer-by-layer order that range spans nearly the whole 104 GiB file,
so Metal runs out of memory even when the routed experts are kept on the CPU (`-cmoe`).
Writing every non-expert tensor first, then the per-layer n-gram table, then the routed
experts, keeps the GPU-side range to the first few GiB.

Usage: uv run --with gguf python gguf_reorder.py <first shard> <output.gguf>
"""

import glob
import re
import sys

import gguf


def group(name):
    if "_exps" in name:
        return 2
    if name.startswith("per_layer_token_embd"):
        return 1
    return 0


def main():
    first, out = sys.argv[1], sys.argv[2]
    pattern = re.sub(r"-\d{5}-of-(\d{5})\.gguf$", r"-*-of-\1.gguf", first)
    shards = sorted(glob.glob(pattern))
    readers = [gguf.GGUFReader(p) for p in shards]
    head = readers[0]
    arch = head.fields[gguf.Keys.General.ARCHITECTURE].contents()
    writer = gguf.GGUFWriter(out, arch=arch, endianess=head.endianess)
    for field in head.fields.values():
        if (
            field.name == gguf.Keys.General.ARCHITECTURE
            or field.name.startswith("GGUF.")
            or field.name.startswith("split.")
        ):
            continue
        val_type = field.types[0]
        sub_type = field.types[-1] if val_type == gguf.GGUFValueType.ARRAY else None
        writer.add_key_value(field.name, field.contents(), val_type, sub_type=sub_type)
    tensors = [(r, t) for r in readers for t in r.tensors]
    tensors.sort(key=lambda rt: group(rt[1].name))
    sizes = [0, 0, 0]
    for _, t in tensors:
        sizes[group(t.name)] += t.n_bytes
        writer.add_tensor_info(
            t.name, t.data.shape, t.data.dtype, t.data.nbytes, t.tensor_type
        )
    print(
        f"{len(shards)} shards, {len(tensors)} tensors; GiB other/ngram/experts:",
        [round(s / 2**30, 1) for s in sizes],
        flush=True,
    )
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_ti_data_to_file()
    for i, (r, t) in enumerate(tensors):
        writer.write_tensor_data(t.data, tensor_endianess=r.endianess)
        if i % 200 == 0:
            print(f"{i}/{len(tensors)}", flush=True)
    writer.close()
    print("done", flush=True)


if __name__ == "__main__":
    main()
