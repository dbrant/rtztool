"""The MADE script bytecode: disassembler and assembler.

Scripts are stored as byte-array objects in the game database.  Each opcode is
one byte followed by its operands; ``W``/``w`` are 16-bit little endian, ``b``
is one byte, ``E`` is an external-function index.

Branch targets are absolute offsets into the script object, which makes
round-tripping easy: the assembler re-resolves labels to the same numbers.
"""

import re
import struct

# opcode (1-based) -> (mnemonic, operand signature)
OPCODES = [
    ("branch_true", "W"), ("branch_false", "W"), ("branch", "W"), ("true", ""),
    ("false", ""), ("push", ""), ("not", ""), ("add", ""),
    ("sub", ""), ("mul", ""), ("div", ""), ("mod", ""),
    ("band", ""), ("bor", ""), ("bnot", ""), ("lt", ""),
    ("eq", ""), ("gt", ""), ("load_constant", "w"), ("load_variable", "w"),
    ("get_property", ""), ("set_property", ""), ("set", "w"), ("print", ""),
    ("terpri", ""), ("print_number", ""), ("vref", ""), ("vset", ""),
    ("vsize", ""), ("exit", ""), ("return", ""), ("call", "b"),
    ("svar", ""), ("sset", ""), ("split", ""), ("snlit", ""),
    ("yorn", ""), ("save", ""), ("restore", ""), ("arg", "b"),
    ("aset", "b"), ("tmp", "b"), ("tset", "b"), ("tspace", "b"),
    ("class", ""), ("objectp", ""), ("vectorp", ""), ("restart", ""),
    ("rand", ""), ("randomize", ""), ("send", "b"), ("extend", "Eb"),
    ("catch", ""), ("cdone", ""), ("throw", ""), ("functionp", ""),
    ("le", ""), ("ge", ""), ("varx", ""), ("setx", ""),
]

MNEMONICS = {name: i + 1 for i, (name, _sig) in enumerate(OPCODES)}
OP_RETURN = MNEMONICS["return"]

# Return to Zork's external function table, in the order the interpreter
# registers it (see ScriptFunctions::setupExternalsTable).
EXTERNALS = [
    "sfSystemCall", "sfInitGraf", "sfRestoreGraf", "sfDrawPicture",
    "sfClearScreen", "sfShowPage", "sfPollEvent", "sfGetMouseX", "sfGetMouseY",
    "sfGetKey", "sfSetVisualEffect", "sfPlaySound", "sfPlayMusic",
    "sfStopMusic", "sfIsMusicPlaying", "sfSetTextPos", "sfFlashScreen",
    "sfPlayNote", "sfStopNote", "sfPlayTele", "sfStopTele",
    "sfHideMouseCursor", "sfShowMouseCursor", "sfGetMusicBeat",
    "sfSetScreenLock", "sfAddSprite", "sfFreeAnim", "sfDrawSprite",
    "sfEraseSprites", "sfUpdateSprites", "sfGetTimer", "sfSetTimer",
    "sfResetTimer", "sfAllocTimer", "sfFreeTimer", "sfSetPaletteLock",
    "sfSetFont", "sfDrawText", "sfHomeText", "sfSetTextRect", "sfSetTextXY",
    "sfSetFontDropShadow", "sfSetFontColor", "sfSetFontOutline",
    "sfLoadMouseCursor", "sfSetSpriteGround", "sfLoadResText",
    "sfSetClipArea", "sfSetSpriteClip",
    "sfSoundPlaying", "sfStopSound", "sfPlayVoice",
    "sfPlayCd", "sfStopCd", "sfGetCdStatus", "sfGetCdTime", "sfPlayCdSegment",
    "sfPrintf", "sfClearMono", "sfGetSoundEnergy", "sfClearText", "sfAnimText",
    "sfGetTextWidth", "sfPlayMovie", "sfLoadSound", "sfLoadMusic",
    "sfLoadPicture", "sfSetMusicVolume", "sfRestartEvents", "sfPlaceSprite",
    "sfPlaceText", "sfDeleteChannel", "sfGetChannelType", "sfSetChannelState",
    "sfSetChannelLocation", "sfSetChannelContent", "sfSetExcludeArea",
    "sfSetSpriteExclude", "sfGetChannelState", "sfPlaceAnim", "sfSetAnimFrame",
    "sfGetAnimFrame", "sfGetAnimFrameCount", "sfGetPictureWidth",
    "sfGetPictureHeight", "sfSetSoundRate", "sfDrawAnimPic", "sfLoadAnim",
    "sfReadText", "sfReadMenu", "sfDrawMenu", "sfGetMenuCount", "sfSaveGame",
    "sfLoadGame", "sfGetGameDescription", "sfShakeScreen", "sfPlaceMenu",
    "sfSetSoundVolume", "sfGetSynthType", "sfIsSlowSystem",
]

EXTERNAL_INDEX = {name: i for i, name in enumerate(EXTERNALS)}


class Instruction(object):
    __slots__ = ("offset", "opcode", "mnemonic", "operands", "size")

    def __init__(self, offset, opcode, mnemonic, operands, size):
        self.offset = offset
        self.opcode = opcode
        self.mnemonic = mnemonic
        self.operands = operands
        self.size = size

    def __repr__(self):
        return "<%04X %s %s>" % (self.offset, self.mnemonic, self.operands)


def decode(code):
    """Walk a script byte array.  Raises ValueError on anything malformed."""
    out = []
    pos = 0
    n = len(code)
    while pos < n:
        start = pos
        opcode = code[pos]
        pos += 1
        if not (1 <= opcode <= len(OPCODES)):
            raise ValueError("bad opcode 0x%02X at 0x%04X" % (opcode, start))
        mnemonic, sig = OPCODES[opcode - 1]
        operands = []
        for ch in sig:
            if ch in "bBE":
                if pos + 1 > n:
                    raise ValueError("truncated operand at 0x%04X" % start)
                operands.append(code[pos])
                pos += 1
            else:
                if pos + 2 > n:
                    raise ValueError("truncated operand at 0x%04X" % start)
                operands.append(struct.unpack_from("<H", code, pos)[0])
                pos += 2
        out.append(Instruction(start, opcode, mnemonic, operands, pos - start))
    return out


def looks_like_code(data):
    """A byte array is a script when it ends in ``return`` and decodes cleanly."""
    if len(data) < 2 or data[-1] != OP_RETURN:
        return False
    try:
        instrs = decode(data)
    except ValueError:
        return False
    if not instrs:
        return False
    # every branch must land on an instruction boundary
    starts = {i.offset for i in instrs}
    starts.add(len(data))
    for ins in instrs:
        if ins.mnemonic in ("branch", "branch_true", "branch_false"):
            if ins.operands[0] not in starts:
                return False
    return True


def disassemble(code, name=None, objects=None, comment_width=48):
    """Render a script as annotated assembly text."""
    instrs = decode(code)
    targets = set()
    for ins in instrs:
        if ins.mnemonic in ("branch", "branch_true", "branch_false"):
            targets.add(ins.operands[0])

    lines = []
    if name:
        lines.append("; %s" % name)
    lines.append(".script %d" % len(code))
    for ins in instrs:
        if ins.offset in targets:
            lines.append("L%04X:" % ins.offset)
        text = "    %-16s" % ins.mnemonic
        args = []
        comment = ""
        for i, ch in enumerate(OPCODES[ins.opcode - 1][1]):
            value = ins.operands[i]
            if ch in "Ww":
                if ins.mnemonic in ("branch", "branch_true", "branch_false"):
                    args.append("L%04X" % value)
                else:
                    args.append(str(value))
            elif ch == "E":
                args.append(EXTERNALS[value] if value < len(EXTERNALS) else "extern%d" % value)
            else:
                args.append(str(value))
        if ins.mnemonic == "load_constant" and objects is not None:
            comment = _object_hint(objects, ins.operands[0])
        text += " ".join(args)
        if comment:
            text = "%-*s ; %s" % (comment_width, text.rstrip(), comment)
        lines.append(text.rstrip())
    return "\n".join(lines) + "\n"


def _object_hint(objects, index):
    obj = objects.get(index) if hasattr(objects, "get") else None
    if obj is None or not obj.is_byte_array:
        return ""
    body = obj.data.split(b"\x00")[0]
    if not body or not all(32 <= b < 127 or b in (9, 10, 13) for b in body):
        return ""
    txt = body.decode("cp850")
    if len(txt) > 60:
        txt = txt[:57] + "..."
    txt = (txt.replace("\\", "\\\\").replace('"', '\\"')
              .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return 'obj %d = "%s"' % (index, txt)


_LABEL_RE = re.compile(r"^L([0-9A-Fa-f]{1,8}):?$")


def assemble(text):
    """Turn disassembly text back into bytes.

    Two passes: the first measures every instruction so labels resolve, the
    second emits.  Label names must be ``Lxxxx``; any other label is rejected
    because branch operands are absolute offsets, not relative ones.
    """
    lines = []
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if line:
            lines.append(line)

    labels = {}
    program = []
    pos = 0
    for line in lines:
        if line.startswith(".script"):
            continue
        if line.endswith(":"):
            m = _LABEL_RE.match(line[:-1] + ":")
            if not m:
                raise ValueError("unsupported label %r" % line)
            labels[line[:-1]] = pos
            continue
        parts = line.split()
        mnemonic = parts[0]
        if mnemonic not in MNEMONICS:
            raise ValueError("unknown mnemonic %r" % mnemonic)
        opcode = MNEMONICS[mnemonic]
        sig = OPCODES[opcode - 1][1]
        args = parts[1:]
        if len(args) != len(sig):
            raise ValueError("%s takes %d operand(s), got %d" % (mnemonic, len(sig), len(args)))
        size = 1 + sum(1 if ch in "bBE" else 2 for ch in sig)
        program.append((pos, opcode, sig, args))
        pos += size

    out = bytearray()
    for _off, opcode, sig, args in program:
        out.append(opcode)
        for ch, arg in zip(sig, args):
            if ch == "E":
                value = EXTERNAL_INDEX.get(arg)
                if value is None:
                    value = int(arg.replace("extern", ""), 0)
                out.append(value & 0xFF)
            elif ch in "bB":
                out.append(int(arg, 0) & 0xFF)
            else:
                if arg in labels:
                    value = labels[arg]
                elif _LABEL_RE.match(arg):
                    raise ValueError("undefined label %r" % arg)
                else:
                    value = int(arg, 0)
                out += struct.pack("<H", value & 0xFFFF)
    return bytes(out)
