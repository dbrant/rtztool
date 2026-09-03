"""Decompile ``RTZCD.DAT`` into editable text, and recompile it.

Produces::

    <outdir>/database.json    header, object table, per-object metadata
    <outdir>/strings.json     every text byte-array, keyed by object index
    <outdir>/scripts/*.asm    one disassembly per code object
    <outdir>/objects.txt      read-only report of the object hierarchy

Only ``strings.json`` and the ``.asm`` files are inputs to :func:`build`;
the rest is documentation.
"""

import json
import os

from . import script
from .dat import GameDatabase, CLASS_BYTE_ARRAY


def extract(dat_path, outdir, encoding="cp850"):
    db = GameDatabase.load(dat_path)
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(os.path.join(outdir, "scripts"), exist_ok=True)

    code = set(db.code_objects())
    text = set(db.text_objects(min_len=2))

    meta = {
        "tool": "rtztool/1",
        "source": os.path.basename(dat_path),
        "encoding": encoding,
        "program_name": db.program_name.rstrip(b"\x00").decode("latin-1"),
        "object_count": len(db.objects),
        "main_code_index": db.main_code_index,
        "game_state_size": len(db.game_state),
        "objects": [],
    }

    strings = {}
    for obj in db.objects:
        entry = {
            "index": obj.index,
            "class": obj.cls,
            "constant": obj.constant,
            "size": len(obj.data),
        }
        if obj.is_byte_array:
            if obj.index in code:
                entry["kind"] = "script"
                path = os.path.join(outdir, "scripts", "%05d.asm" % obj.index)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(script.disassemble(
                        obj.data, name="object %d%s" % (obj.index,
                                                        "" if obj.constant else " (variable)"),
                        objects=db))
                entry["file"] = "scripts/%05d.asm" % obj.index
            elif obj.index in text:
                entry["kind"] = "text"
                strings[str(obj.index)] = _decode_string(obj.data, encoding)
            else:
                entry["kind"] = "bytes"
                entry["hex"] = obj.data.hex()
        elif obj.is_word_array:
            entry["kind"] = "words"
            entry["values"] = obj.words()
        else:
            entry["kind"] = "object"
            entry["parent"] = obj.cls
            entry["count1"] = obj.count1
            entry["count2"] = obj.count2
            entry["properties"] = [
                {"id": pid, "flags": flags, "value": value}
                for pid, flags, value in obj.properties()
            ]
        meta["objects"].append(entry)

    with open(os.path.join(outdir, "database.json"), "w") as f:
        json.dump(meta, f, indent=1)
    with open(os.path.join(outdir, "strings.json"), "w", encoding="utf-8") as f:
        json.dump(strings, f, indent=1, ensure_ascii=False, sort_keys=True)

    with open(os.path.join(outdir, "objects.txt"), "w", encoding="utf-8") as f:
        for obj in db.objects:
            if not obj.is_object:
                continue
            f.write("object %d  parent=%d  slots=%d/%d%s\n" % (
                obj.index, obj.cls, obj.count1, obj.count2,
                "" if obj.constant else "  (variable)"))
            for pid, flags, value in obj.properties():
                note = ""
                target = db.get(value)
                if target is not None and target.is_byte_array and value in text:
                    note = "  ; %r" % target.text(encoding)[:50]
                elif value in code:
                    note = "  ; script"
                f.write("    prop %-5d flags=%04X value=%-6d%s\n" % (pid, flags, value, note))
            f.write("\n")

    return {"objects": len(db.objects), "scripts": len(code), "strings": len(strings)}


def build(indir, source_dat, out_dat):
    with open(os.path.join(indir, "database.json")) as f:
        meta = json.load(f)
    encoding = meta.get("encoding", "cp850")
    with open(os.path.join(indir, "strings.json"), encoding="utf-8") as f:
        strings = json.load(f)

    db = GameDatabase.load(source_dat)
    changed = []

    for entry in meta["objects"]:
        index = entry["index"]
        obj = db.get(index)
        if obj is None:
            raise ValueError("database.json refers to unknown object %d" % index)
        kind = entry.get("kind")
        if kind == "script":
            path = os.path.join(indir, entry["file"])
            with open(path, encoding="utf-8") as f:
                text = f.read()
            data = script.assemble(text)
            if data != obj.data:
                obj.data = data
                changed.append(index)
        elif kind == "text":
            new = strings.get(str(index))
            if new is None:
                continue
            data = _encode_string(new, encoding, len(obj.data))
            if data != obj.data:
                obj.data = data
                changed.append(index)

    size, moved = db.save(out_dat)
    return {"changed": changed, "moved": moved, "size": size}


def _decode_string(data, encoding):
    """Keep the exact bytes recoverable: trailing NULs are recorded as padding."""
    body = data
    nuls = 0
    while body and body[-1] == 0:
        body = body[:-1]
        nuls += 1
    text = body.decode(encoding, "replace")
    if nuls != 1:
        return {"text": text, "pad": nuls}
    return text


def _encode_string(value, encoding, original_size):
    if isinstance(value, dict):
        text = value.get("text", "")
        pad = value.get("pad", 1)
    else:
        text = value
        pad = 1
    raw = text.encode(encoding, "replace") + b"\x00" * pad
    return raw
