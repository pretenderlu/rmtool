"""Inspect official Qt symbols and existing IME field offsets, without execution."""

import argparse
import hashlib
import json
import sys
from pathlib import Path


def inspect(root):
    from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM
    from elftools.elf.elffile import ELFFile

    process = "_ZN22QGuiApplicationPrivate15processKeyEventEPN29QWindowSystemInterfacePrivate8KeyEventE"
    required = {
        "Core": ("_ZN11QTranslator4loadERK7QLocaleRK7QStringS5_S5_S5_",
                 "_ZN11QTranslator4loadERK7QStringS2_S2_S2_",
                 "_ZN7QStringC1EPK5QCharx", "_ZNK7QLocale8languageEv"),
        "Gui": (process, "_ZN17QInputMethodEvent15setCommitStringERK7QStringii"),
    }
    result = {}
    for library, symbols in required.items():
        path = root / f"usr/lib/libQt6{library}.so.6.10.3"
        with path.open("rb") as stream:
            elf = ELFFile(stream)
            if elf["e_machine"] != "EM_AARCH64":
                raise RuntimeError("Expected AArch64 Qt")
            table = elf.get_section_by_name(".dynsym")
            for name in symbols:
                matches = table.get_symbol_by_name(name) or []
                if len(matches) != 1 or matches[0]["st_shndx"] == "SHN_UNDEF":
                    raise RuntimeError(f"Required Qt export absent: {name}")
            entry = dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), symbols=symbols)
            if library == "Gui":
                symbol = table.get_symbol_by_name(process)[0]
                section = elf.get_section(symbol["st_shndx"])
                start = symbol["st_value"] - section["sh_addr"]
                code = section.data()[start:start + symbol["st_size"]]
                instructions = [(i.mnemonic, i.op_str) for i in Cs(CS_ARCH_ARM64, CS_MODE_ARM).disasm(code, symbol["st_value"])]
                # Exact observed instructions in .172, not inferred offsets from a release label.
                evidence = [("mov", "x19, x0"), ("ldr", "w23, [x19, #0x48]"),
                            ("add", "x28, x19, #0x50"), ("ldp", "w22, w24, [x19, #0x6c]")]
                if any(item not in instructions for item in evidence):
                    raise RuntimeError("IME KeyEvent field evidence differs; manual ABI review required")
                entry.update(key_event_offsets=dict(key=72, unicode=80, key_type=108),
                             symbol_address=hex(symbol["st_value"]), instructions=evidence)
            result[library] = entry
    return result


def inspect_native(path):
    from elftools.elf.elffile import ELFFile
    from elftools.elf.relocation import RelocationSection

    with path.open("rb") as stream:
        elf = ELFFile(stream)
        if elf["e_machine"] != "EM_AARCH64" or elf["e_type"] != "ET_DYN":
            raise RuntimeError("Unexpected native translator ELF")
        symbols = elf.get_section_by_name(".dynsym")
        if any(s.name and s["st_shndx"] == "SHN_UNDEF" for s in symbols.iter_symbols()):
            raise RuntimeError("Native translator has unresolved dynamic imports")
        relocs = []
        for section in elf.iter_sections():
            if not isinstance(section, RelocationSection):
                continue
            table = elf.get_section(section["sh_link"])
            for relocation in section.iter_relocations():
                kind = relocation["r_info_type"]
                index = relocation["r_info_sym"]
                if kind not in {257, 1025, 1027}:
                    raise RuntimeError("Unexpected native translator relocation")
                if index and table.get_symbol(index)["st_shndx"] == "SHN_UNDEF":
                    raise RuntimeError("Unresolved native translator relocation")
                offset = relocation["r_offset"]
                if not any(p["p_type"] == "PT_LOAD" and p["p_vaddr"] <= offset
                           and offset + 8 <= p["p_vaddr"] + p["p_memsz"] for p in elf.iter_segments()):
                    raise RuntimeError("Native relocation destination outside mapped memory")
                relocs.append(dict(type=kind, symbol=table.get_symbol(index).name if index else None))
        if len(relocs) != 3:
            raise RuntimeError("Native translator relocation count changed")
    return dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), relocations=relocs,
                resolution="Standard AArch64 RELATIVE/ABS64/GLOB_DAT; all symbol relocations internal. Xovi Qt imports checked separately.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-libs", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.python_libs))
    result = {platform: inspect(args.root / platform) for platform in ("ferrari", "chiappa")}
    result["native_translator"] = inspect_native(Path(__file__).resolve().parents[1] / "native-chinese/native-chinese-translator.so")
    result["scope"] = "Static Qt export and IME field checks only; no runtime/device validation"
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
