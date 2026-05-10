"""Phase 3b precondition — probe vc4's plane capabilities on the dev Pi.

Before designing the multi-plane atomic compositor for HDMI 1080p
(qarl 2026-05-02: motion + composite must be GPU-side at full res),
nail down what vc4-kms-v3d actually exposes:

  1. How many overlay planes are bound to our CRTC?
  2. What pixel formats does each overlay plane support? (need
     ARGB8888 at 1080p without HVS scaling on the static-text plane,
     and on each animated layer's plane.)
  3. What atomic properties does each overlay plane expose? (need
     CRTC_X/Y/W/H, FB_ID, CRTC_ID, SRC_X/Y/W/H — those are
     guaranteed; what we're looking for here is "alpha" for the
     pulse effect, plus anything that hints at limits we'd hit.)
  4. Sanity: total planes, primaries, overlays, cursors.

Output is a markdown report so QA can paste it into the verifier
notes. No /dev/dri write; no DRM master grab needed beyond the
read-only ioctls. Run as the openmarquee user on the Pi:

    cd /home/openmarquee/openmarquee
    PYTHONPATH=backend python3 scripts/probes/phase6_vc4_probe.py
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import sys
from pathlib import Path

# scripts/probes/<this file> -> ../../backend (parent.parent.parent).
ROOT = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(ROOT))

# Reuse the ctypes structs + ioctl numbers we already validated in
# drm_kms.py. They land at the same kernel ABI on this Pi.
from openmarquee.rendering.drm_kms import (  # noqa: E402
    DRM_CLIENT_CAP_ATOMIC,
    DRM_CLIENT_CAP_UNIVERSAL_PLANES,
    DRM_IOCTL_MODE_GETPLANE,
    DRM_IOCTL_MODE_GETPLANERESOURCES,
    DRM_IOCTL_MODE_GETPROPERTY,
    DRM_IOCTL_MODE_GETRESOURCES,
    DRM_IOCTL_MODE_OBJ_GETPROPERTIES,
    DRM_IOCTL_SET_CLIENT_CAP,
    DRM_MODE_OBJECT_PLANE,
    DRM_PLANE_TYPE_CURSOR,
    DRM_PLANE_TYPE_OVERLAY,
    DRM_PLANE_TYPE_PRIMARY,
    _DrmModeGetPlane,
    _DrmModeGetPlaneRes,
    _DrmModeGetProperty,
    _DrmModeObjGetProperties,
    _DrmModeRes,
    _DrmSetClientCap,
)


def _ioctl(fd, request, arg):
    fcntl.ioctl(fd, request, arg)


def _fourcc_str(code: int) -> str:
    return "".join(chr((code >> (8 * i)) & 0xFF) for i in range(4))


def _set_client_caps(fd: int) -> None:
    for cap in (DRM_CLIENT_CAP_UNIVERSAL_PLANES, DRM_CLIENT_CAP_ATOMIC):
        c = _DrmSetClientCap(capability=cap, value=1)
        _ioctl(fd, DRM_IOCTL_SET_CLIENT_CAP, c)


def _get_plane_props(fd: int, plane_id: int) -> list[tuple[str, int]]:
    obj = _DrmModeObjGetProperties()
    obj.obj_id = plane_id
    obj.obj_type = DRM_MODE_OBJECT_PLANE
    _ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, obj)
    if obj.count_props == 0:
        return []
    prop_ids = (ctypes.c_uint32 * obj.count_props)()
    prop_vals = (ctypes.c_uint64 * obj.count_props)()
    obj.props_ptr = ctypes.cast(prop_ids, ctypes.c_void_p).value or 0
    obj.prop_values_ptr = ctypes.cast(prop_vals, ctypes.c_void_p).value or 0
    _ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, obj)
    out = []
    for i in range(obj.count_props):
        prop = _DrmModeGetProperty()
        prop.prop_id = prop_ids[i]
        _ioctl(fd, DRM_IOCTL_MODE_GETPROPERTY, prop)
        name = prop.name.decode("ascii", errors="ignore").rstrip("\x00")
        out.append((name, int(prop_vals[i])))
    return out


def _get_plane_formats(fd: int, plane_id: int) -> tuple[int, list[str]]:
    """Return (possible_crtcs_bitmask, [fourcc_str, ...]) for the plane."""
    plane = _DrmModeGetPlane()
    plane.plane_id = plane_id
    _ioctl(fd, DRM_IOCTL_MODE_GETPLANE, plane)
    if plane.count_format_types == 0:
        return (int(plane.possible_crtcs), [])
    formats = (ctypes.c_uint32 * plane.count_format_types)()
    plane.format_type_ptr = ctypes.cast(formats, ctypes.c_void_p).value or 0
    _ioctl(fd, DRM_IOCTL_MODE_GETPLANE, plane)
    return (
        int(plane.possible_crtcs),
        [_fourcc_str(int(f)) for f in formats],
    )


def main() -> int:
    card = Path("/dev/dri/card0")
    if not card.exists():
        print(f"ERR: {card} missing", file=sys.stderr)
        return 1
    fd = os.open(card, os.O_RDWR | os.O_CLOEXEC)
    try:
        _set_client_caps(fd)

        # GETRESOURCES — count CRTCs so we can interpret possible_crtcs
        # bitmasks meaningfully.
        res = _DrmModeRes()
        _ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, res)
        crtcs_arr = (ctypes.c_uint32 * res.count_crtcs)()
        res.crtc_id_ptr = ctypes.cast(crtcs_arr, ctypes.c_void_p).value or 0
        res.count_fbs = 0
        res.count_connectors = 0
        res.count_encoders = 0
        _ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, res)
        crtc_ids = list(crtcs_arr)
        print(f"# vc4 plane probe — {card}\n")
        print(f"CRTCs: {len(crtc_ids)} → ids {crtc_ids}\n")

        # Plane resources.
        pres = _DrmModeGetPlaneRes()
        _ioctl(fd, DRM_IOCTL_MODE_GETPLANERESOURCES, pres)
        plane_ids = (ctypes.c_uint32 * pres.count_planes)()
        pres.plane_id_ptr = ctypes.cast(plane_ids, ctypes.c_void_p).value or 0
        _ioctl(fd, DRM_IOCTL_MODE_GETPLANERESOURCES, pres)
        plane_ids = list(plane_ids)
        print(f"Total planes: {len(plane_ids)}\n")

        type_names = {
            DRM_PLANE_TYPE_PRIMARY: "PRIMARY",
            DRM_PLANE_TYPE_OVERLAY: "OVERLAY",
            DRM_PLANE_TYPE_CURSOR: "CURSOR",
        }

        # Per-CRTC plane bucketing.
        per_crtc: dict[int, dict[str, list[int]]] = {
            cid: {"PRIMARY": [], "OVERLAY": [], "CURSOR": []} for cid in crtc_ids
        }

        all_props: dict[int, list[tuple[str, int]]] = {}
        all_formats: dict[int, list[str]] = {}
        all_possible: dict[int, int] = {}
        all_types: dict[int, str] = {}

        for pid in plane_ids:
            possible, formats = _get_plane_formats(fd, pid)
            props = _get_plane_props(fd, pid)
            type_val = -1
            for name, val in props:
                if name == "type":
                    type_val = int(val)
                    break
            type_label = type_names.get(type_val, f"UNKNOWN({type_val})")
            all_props[pid] = props
            all_formats[pid] = formats
            all_possible[pid] = possible
            all_types[pid] = type_label
            for i, cid in enumerate(crtc_ids):
                if possible & (1 << i):
                    per_crtc[cid][type_label].append(pid)

        # Per-CRTC summary table.
        print("## planes per CRTC (filtered by possible_crtcs)\n")
        print("| crtc_id | PRIMARY | OVERLAY | CURSOR |")
        print("|---------|---------|---------|--------|")
        for cid in crtc_ids:
            pri = per_crtc[cid]["PRIMARY"]
            ovr = per_crtc[cid]["OVERLAY"]
            cur = per_crtc[cid]["CURSOR"]
            print(f"| {cid:>7} | {len(pri):>7} | {len(ovr):>7} | {len(cur):>6} |")
        print()

        # Per-plane detail.
        print("## per-plane detail\n")
        for pid in plane_ids:
            ptype = all_types[pid]
            possible = all_possible[pid]
            crtc_bits = [
                str(crtc_ids[i])
                for i in range(len(crtc_ids))
                if possible & (1 << i)
            ]
            print(f"### plane {pid} — {ptype}")
            print(f"  possible_crtcs (ids): {', '.join(crtc_bits) or '(none)'}")
            formats = all_formats[pid]
            print(f"  formats ({len(formats)}): {', '.join(formats)}")
            prop_names = [n for n, _ in all_props[pid]]
            has_alpha = "alpha" in prop_names
            has_pixel_blend = any(
                n.lower().startswith("pixel") and "blend" in n.lower()
                for n in prop_names
            )
            has_zpos = any(n.lower() in ("zpos", "z-pos") for n in prop_names)
            print(f"  alpha prop: {'YES' if has_alpha else 'NO'}    "
                  f"pixel_blend prop: {'YES' if has_pixel_blend else 'NO'}    "
                  f"zpos prop: {'YES' if has_zpos else 'NO'}")
            print("  properties:")
            for name, val in all_props[pid]:
                print(f"    - {name} = {val}")
            print()

        # Headline: the four questions Phase 3b's design needs answered.
        print("## design questions answered\n")
        # 1. Plane count per HDMI CRTC.
        # Find the CRTC with at least one connected primary plane that
        # also has overlays — that's where HDMI lives.
        hdmi_crtc = None
        for cid in crtc_ids:
            if per_crtc[cid]["PRIMARY"] and per_crtc[cid]["OVERLAY"]:
                hdmi_crtc = cid
                break
        if hdmi_crtc is None and crtc_ids:
            hdmi_crtc = crtc_ids[0]
        if hdmi_crtc is not None:
            ovr = per_crtc[hdmi_crtc]["OVERLAY"]
            print(f"- **Q1 plane count on the HDMI CRTC ({hdmi_crtc}):** "
                  f"{len(per_crtc[hdmi_crtc]['PRIMARY'])} primary + "
                  f"{len(ovr)} overlay + "
                  f"{len(per_crtc[hdmi_crtc]['CURSOR'])} cursor.")
            print(f"  Plane budget for the multi-plane compositor: "
                  f"primary (bg) + 1 static-text overlay + up to "
                  f"{max(0, len(ovr) - 1)} animated-layer overlays.")
            # 2. ARGB8888 support on overlay planes.
            argb_ok = True
            for opid in ovr:
                if "AR24" not in all_formats[opid]:
                    argb_ok = False
                    print(f"  - overlay plane {opid} formats: "
                          f"{all_formats[opid]} (NO AR24/ARGB8888!)")
            if argb_ok:
                print(f"- **Q2 ARGB8888 on overlays:** YES on all "
                      f"{len(ovr)} overlay planes.")
            else:
                print(f"- **Q2 ARGB8888 on overlays:** PARTIAL — see "
                      f"per-plane format detail above.")
            # 3. Alpha property on overlay planes.
            alpha_ok = all(
                any(n == "alpha" for n, _ in all_props[opid]) for opid in ovr
            )
            pixel_blend_ok = all(
                any(n.lower().startswith("pixel") and "blend" in n.lower()
                    for n, _ in all_props[opid])
                for opid in ovr
            )
            print(f"- **Q3 per-plane alpha property:** "
                  f"{'YES' if alpha_ok else 'NO'} on all overlays.")
            print(f"  pixel-blend mode property: "
                  f"{'YES' if pixel_blend_ok else 'NO'} on all overlays "
                  f"(useful for premultiplied-alpha compositing).")
            # 4. zpos for stacking order
            zpos_ok = all(
                any(n.lower() in ("zpos", "z-pos") for n, _ in all_props[opid])
                for opid in ovr
            )
            print(f"- **Q4 zpos / stacking order property:** "
                  f"{'YES' if zpos_ok else 'NO'} on all overlays.")
        else:
            print("- (no CRTCs found, can't summarize)")
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
