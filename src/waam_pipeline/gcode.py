from __future__ import annotations

import re

from .toolpath import Move, Toolpath

_WORD = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")


_INERT_M_CODES = frozenset({
    0.0, 1.0,
    17.0, 18.0, 84.0,
    73.0,
    104.0, 105.0, 106.0, 107.0, 109.0,
    117.0,
    140.0, 190.0,
    400.0,
})


_INERT_G_CODES = frozenset({4.0})


_BRACKET = re.compile(r"\[[^\]]*\]")


_VALID_LETTERS = frozenset("GMXYZEFSPNIJKRT")


_NUMBER_INTO_E = re.compile(r"[\d.][eE][-+]?[\d.]")
_LAYER_COMMENT = re.compile(r"\bLAYER\s*[:=]\s*(-?\d+)", re.IGNORECASE)
_TINY = 1e-9
_MOVE_WORDS = frozenset("XYZEFN")


class ParseError(ValueError):
    def __init__(self, line_no: int, message: str) -> None:
        super().__init__(f"G-code line {line_no}: {message}")
        self.line_no = line_no


def _split_comment(raw: str) -> tuple[str, str]:

    comments: list[str] = []

    def _paren(match: re.Match[str]) -> str:
        comments.append(match.group(1))
        return " "

    code = re.sub(r"\(([^)]*)\)", _paren, raw)
    if (i := code.find(";")) >= 0:
        comments.append(code[i + 1 :])
        code = code[:i]
    return code.strip(), " ".join(c.strip() for c in comments)


def parse(text: str, arc_on: str | None = None, arc_off: str | None = None) -> Toolpath:


    tp = Toolpath()
    arc_state = False
    x = y = z = e = 0.0
    feed: float | None = None
    scale = 1.0
    absolute = True
    e_absolute = True
    motion: int | None = None
    layer = 0
    saw_layer_comments = False
    last_deposit_z: float | None = None

    for line_no, raw in enumerate(text.splitlines(), 1):
        code, comment = _split_comment(raw)
        if (m := _LAYER_COMMENT.search(comment)) is not None:
            layer = int(m.group(1))
            saw_layer_comments = True
        if not code:
            continue


        code = _BRACKET.sub(" ", code)
        if (amb := _NUMBER_INTO_E.search(code)) is not None:
            raise ParseError(
                line_no,
                f"ambiguous number {code[max(0, amb.start() - 6):amb.end() + 3]!r} in "
                f"{code!r}: it reads as scientific notation or as a separate E word. "
                "Write coordinates without exponents and put a space before E.",
            )
        spans = [m.span() for m in _WORD.finditer(code)]
        consumed = bytearray(len(code))
        for a, b in spans:
            consumed[a:b] = b"\x01" * (b - a)
        leftover = "".join(
            ch for k, ch in enumerate(code) if not consumed[k] and not ch.isspace()
        )
        if leftover:
            raise ParseError(
                line_no,
                f"unparsed characters {leftover!r} in {code!r}. "
                "Correct the command before importing it.",
            )
        words = _WORD.findall(code)
        if not words:
            raise ParseError(line_no, f"unrecognized text: {code!r}")


        commands: list[tuple[str | None, dict[str, float]]] = []
        cmd: str | None = None
        params: dict[str, float] = {}
        fresh = True
        for letter, value in words:
            letter = letter.upper()
            if letter not in _VALID_LETTERS:
                raise ParseError(
                    line_no,
                    f"unknown word letter {letter!r} in {code!r}. "
                    "Check the coordinate values and command syntax.",
                )
            if letter in "GM":
                if not fresh:
                    commands.append((cmd, params))
                    params = {}
                cmd = f"{letter}{value}"
                fresh = False
            else:
                params[letter] = float(value)
                fresh = False
        commands.append((cmd, params))

        for cmd, params in commands:
            if cmd is not None and arc_on is not None and cmd.upper() == arc_on.upper():
                arc_state = True
                continue
            if cmd is not None and arc_off is not None and cmd.upper() == arc_off.upper():
                arc_state = False
                continue
            if cmd is not None and cmd[0] == "M":
                number = float(cmd[1:])
                if number == 82:
                    e_absolute = True
                elif number == 83:
                    e_absolute = False
                elif number in _INERT_M_CODES:
                    tp.unhandled.append((line_no, cmd))
                else:
                    raise ParseError(
                        line_no,
                        f"unknown M-code {cmd}: refusing to assume it does not move "
                        "the torch or change the extrusion frame. Add it to "
                        "_INERT_M_CODES once you have checked what it does.",
                    )
                continue

            if cmd is None or (cmd[0] == "G" and float(cmd[1:]) in (0.0, 1.0)):


                if params.get("T") == 0.0:
                    tp.unhandled.append((line_no, "T0"))
                    params = {k: v for k, v in params.items() if k != "T"}
                    if cmd is None and not params:
                        continue
                stray = sorted(params.keys() - _MOVE_WORDS)
                if stray:
                    raise ParseError(
                        line_no,
                        f"{'/'.join(stray)} word on a {cmd or 'bare'} line in {code!r}: "
                        "a tool change, power or dwell word here is not modelled, "
                        "and this parser will not drop it. Remove it from the source.",
                    )
            if cmd is None:
                if motion is None:
                    raise ParseError(line_no, "coordinate words with no active G0/G1")
                number = float(motion)
            else:
                number = float(cmd[1:])

            if number in (0.0, 1.0):
                motion = int(number)
                if "F" in params:
                    feed = params["F"] * scale
                if not ({"X", "Y", "Z", "E"} & params.keys()):
                    continue
                if absolute:
                    x = x if "X" not in params else params["X"] * scale
                    y = y if "Y" not in params else params["Y"] * scale
                    z = z if "Z" not in params else params["Z"] * scale
                else:
                    x += params.get("X", 0.0) * scale
                    y += params.get("Y", 0.0) * scale
                    z += params.get("Z", 0.0) * scale
                de = 0.0
                if "E" in params:
                    ev = params["E"] * scale
                    if e_absolute:
                        de, e = ev - e, ev
                    else:
                        de = ev
                        e += ev
                deposit = arc_state if arc_on is not None else de > _TINY
                if deposit and not saw_layer_comments:
                    if last_deposit_z is None:
                        last_deposit_z = z
                    elif z > last_deposit_z + 1e-6:
                        layer += 1
                        last_deposit_z = z
                tp.moves.append(
                    Move(x=x, y=y, z=z, feed=feed, deposit=deposit, layer=layer, line_no=line_no)
                )
            elif number in (2.0, 3.0):
                raise ParseError(
                    line_no,
                    "G2/G3 arc moves are not supported. Disable arc fitting in the slicer.",
                )
            elif number == 20.0:
                scale = 25.4
            elif number == 21.0:
                scale = 1.0
            elif number == 90.0:
                absolute = e_absolute = True
            elif number == 91.0:
                absolute = e_absolute = False
            elif number == 92.0 and params:


                axes = sorted({"X", "Y", "Z"} & params.keys())
                if axes:
                    raise ParseError(
                        line_no,
                        f"G92 {'/'.join(axes)} moves the coordinate frame without "
                        "recording an offset, so the moves before and after it "
                        "are in different frames. Re-slice in one frame; G92 E "
                        "(the extruder reset) is accepted.",
                    )
                if "E" in params:
                    e = params["E"] * scale
            elif number in _INERT_G_CODES:
                tp.unhandled.append((line_no, code))
            else:


                raise ParseError(
                    line_no,
                    f"unsupported G-code {cmd or 'G' + str(number)}: it may move the "
                    "torch or change the coordinate frame, and this parser will not "
                    "guess. Remove it from the source or add explicit handling.",
                )
    return tp
