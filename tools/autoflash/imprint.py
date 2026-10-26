"""Write Wi-Fi credentials into a prebuilt firmware image.

The plug spends a few minutes running a small startup firmware that has nowhere
to read Wi-Fi details from, so those details have to be inside the image. Asking
every user to compile one is a wall most people will not climb.

Instead the image is built once with padded placeholders in place of the network
name and password, and this writes the real values into that reserved space. The
result is a valid ESP32 application image: both integrity fields the bootloader
checks - the one-byte XOR over segment data and the appended SHA-256 - are
recalculated afterwards.

Nothing here relaxes a check. The patched image is validated exactly as any
downloaded artifact is, and the migration still refuses it unless the plug reads
back what was sent.
"""

from __future__ import annotations

import hashlib
import re

try:
    from tools.analyze_vendor_ota import parse_esp_image
except ModuleNotFoundError:  # running from inside tools/
    from analyze_vendor_ota import parse_esp_image


class ImprintError(RuntimeError):
    pass


# Written into the image at build time. Each is followed by enough reserved
# bytes to hold the longest value 802.11 allows, so the real value never needs
# more room than the placeholder already occupies.
#
# The marker is the start of the C string the firmware reads, not a label in
# front of it, so a value must be written from the marker onwards. Writing it
# after the marker leaves the firmware with "AUTOFLASH-IMPRINT-SSID>YourNetwork"
# as its network name - which is what the first hardware run found.
SSID_MARKER = b"AUTOFLASH-IMPRINT-SSID>"
PASSWORD_MARKER = b"AUTOFLASH-IMPRINT-PASS>"
# The pair images built before the markers were the tool's own carry: ESPHome
# 1.1.0 for the S60, and that config as Home Assistant builds it. Found and
# filled just the same.
LEGACY_MARKERS = (b"S60-IMPRINT-SSID>", b"S60-IMPRINT-PASS>")
SSID_FIELD_BYTES = 33  # 32 characters plus a terminator
PASSWORD_FIELD_BYTES = 64  # 63 characters plus a terminator
# The reserved space is filled with this at build time. It must not be NUL, or
# the compiler would end the string literal early and allocate too little.
FILLER = b"."


def markers_of(image: bytes) -> tuple[bytes, bytes]:
    """The pair of markers `image` was built with: the tool's, or the earlier
    pair.  The tool's when it has neither, so that is the pair errors name."""
    for pair in ((SSID_MARKER, PASSWORD_MARKER), LEGACY_MARKERS):
        if pair[0] in image or pair[1] in image:
            return pair
    return SSID_MARKER, PASSWORD_MARKER


def field_span(marker: bytes, field_bytes: int) -> int:
    """Bytes the firmware's string occupies: the marker plus the reserved filler."""
    return len(marker) + field_bytes


def find_field(image: bytes, marker: bytes, field_bytes: int) -> int:
    """Offset of the string that begins with `marker`, which appears exactly once.

    This is where the firmware's string starts, so it is where a value goes.
    """
    first = image.find(marker)
    if first < 0:
        raise ImprintError(
            f"this image has no {marker.decode()} placeholder, so it was not "
            "built to have credentials written into it"
        )
    if image.find(marker, first + 1) >= 0:
        raise ImprintError(
            f"{marker.decode()} appears more than once; refusing to guess which "
            "one holds the credential"
        )
    if first + field_span(marker, field_bytes) > len(image):
        raise ImprintError(f"{marker.decode()} is too close to the end of the image")
    return first


def write_field(image: bytearray, offset: int, value: str, marker: bytes, field_bytes: int) -> None:
    """Overwrite the whole placeholder string, marker included, NUL-padded."""
    encoded = value.encode("utf-8")
    if len(encoded) >= field_bytes:
        raise ImprintError(
            f"{len(encoded)} bytes is too long for this field, which reserves "
            f"{field_bytes - 1} characters"
        )
    span = field_span(marker, field_bytes)
    image[offset : offset + span] = encoded.ljust(span, b"\0")


def read_c_string(image: bytes, offset: int) -> bytes:
    """The NUL-terminated string at `offset`, as the firmware would read it."""
    end = image.find(b"\0", offset)
    return image[offset : end if end >= 0 else len(image)]


def repair_image(image: bytes) -> bytes:
    """Recompute the two integrity fields the ESP32 bootloader verifies."""
    report = parse_esp_image(image)
    patched = bytearray(image)

    checksum = 0xEF
    for segment in report["segments"]:
        start = segment["data_offset"]
        for value in patched[start : start + segment["size"]]:
            checksum ^= value
    patched[report["checksum_offset"]] = checksum

    if report["hash_appended"]:
        end = report["checksum_offset"] + 1
        patched[end : end + 32] = hashlib.sha256(bytes(patched[:end])).digest()
    return bytes(patched)


def imprint(image: bytes, ssid: str, password: str) -> bytes:
    """Return the image with these credentials written in and its hashes fixed.

    `image` must be the published template: the markers are overwritten, so an
    imprinted copy cannot be imprinted again. Start from the template instead.
    """
    if not ssid:
        raise ImprintError("a network name is required")
    ssid_marker, password_marker = markers_of(image)
    ssid_at = find_field(image, ssid_marker, SSID_FIELD_BYTES)
    password_at = find_field(image, password_marker, PASSWORD_FIELD_BYTES)
    if not has_placeholders(image):
        raise ImprintError(
            "this image's credential placeholders have already been written to, "
            "so it was not built to have credentials written into it - imprint "
            "the published template, not a copy that has already been written"
        )

    patched = bytearray(image)
    write_field(patched, ssid_at, ssid, ssid_marker, SSID_FIELD_BYTES)
    write_field(patched, password_at, password, password_marker, PASSWORD_FIELD_BYTES)
    result = repair_image(bytes(patched))

    # Read back exactly what the firmware will read, from where it reads it.
    if read_c_string(result, ssid_at) != ssid.encode("utf-8"):
        raise ImprintError("the network name the firmware would read is not the one given")
    if read_c_string(result, password_at) != password.encode("utf-8"):
        raise ImprintError("the password the firmware would read is not the one given")

    report = parse_esp_image(result)
    if not report["checksum_valid"] or report["sha256_valid"] is False:
        raise ImprintError("the patched image failed its own validation")
    if len(result) != len(image):
        raise ImprintError("patching changed the image length")
    return result


BUILD_PATH_PATTERN = re.compile(rb"/(?:home|Users)/[A-Za-z0-9._-]+/[^\x00]{0,200}")


def find_build_paths(image: bytes) -> list[bytes]:
    """Absolute paths from the build machine left in the image.

    Compilers bake __FILE__ into assertion and log strings. On a personal
    machine those carry a username and directory layout, which has no business
    being in a published binary.
    """
    return sorted({match.group(0) for match in BUILD_PATH_PATTERN.finditer(image)})


def redact_build_paths(image: bytes, replacement: bytes = b"/build") -> bytes:
    """Overwrite build paths in place, then repair the image.

    The replacement is written into the same bytes and NUL-padded, so the string
    stays valid and nothing moves - only a log message becomes less specific.
    """
    patched = bytearray(image)
    for path in find_build_paths(bytes(image)):
        start = 0
        while True:
            at = patched.find(path, start)
            if at < 0:
                break
            tail = path.rsplit(b"/", 1)[-1]
            neutral = (replacement + b"/" + tail)[: len(path)]
            patched[at : at + len(path)] = neutral.ljust(len(path), b"\0")
            start = at + len(path)
    return repair_image(bytes(patched))


def has_placeholders(image: bytes) -> bool:
    """True when both placeholder strings are present and untouched."""
    ssid_marker, password_marker = markers_of(image)
    for marker, field in ((ssid_marker, SSID_FIELD_BYTES), (password_marker, PASSWORD_FIELD_BYTES)):
        try:
            at = find_field(image, marker, field)
        except ImprintError:
            return False
        if image[at : at + field_span(marker, field)] != marker + FILLER * field:
            return False
    return True


def require_imprinted(image: bytes) -> None:
    """Refuse an image whose firmware would not read the written credentials.

    Such an image is not merely useless: it would boot looking for a network
    named after the placeholder, retry forever, and never raise an access point,
    leaving the plug unreachable at the one moment it must not be. Any marker
    left in the image means exactly that, whether the filler behind it is
    untouched or a value was written after it by the earlier, faulty tool.
    """
    if has_placeholders(image):
        raise ImprintError(
            "this image still has its credential placeholders - it has not had "
            "a network name and password written into it, and installing it "
            "would leave the plug unable to reach any network"
        )
    for marker in (SSID_MARKER, PASSWORD_MARKER, *LEGACY_MARKERS):
        if marker in image:
            raise ImprintError(
                f"{marker.decode()} is still in this image, so the firmware would "
                "read it as part of the network name or password and never join "
                "your Wi-Fi - rebuild the bundle with the current tool"
            )


def credential_regions(published: bytes) -> list[tuple[int, int]]:
    """(offset, length) of each placeholder string in the published template.

    Located in the template because imprinting overwrites the markers.
    """
    ssid_marker, password_marker = markers_of(published)
    return [
        (find_field(published, marker, field), field_span(marker, field))
        for marker, field in ((ssid_marker, SSID_FIELD_BYTES), (password_marker, PASSWORD_FIELD_BYTES))
    ]


def blank_variable_regions(image: bytes, regions: list[tuple[int, int]]) -> bytes:
    """The image with credentials and integrity fields zeroed.

    Two copies of the same published firmware, imprinted with different
    credentials, differ only in the reserved fields and in the checksum and hash
    that cover them. Blanking those makes the rest directly comparable.
    """
    blanked = bytearray(image)
    for at, length in regions:
        blanked[at : at + length] = b"\0" * length
    report = parse_esp_image(image)
    blanked[report["checksum_offset"]] = 0
    if report["hash_appended"]:
        end = report["checksum_offset"] + 1
        blanked[end : end + 32] = b"\0" * 32
    return bytes(blanked)


def derived_from(candidate: bytes, published: bytes) -> bool:
    """True when `candidate` is `published` with credentials written into it.

    Proves provenance without needing the credentials: everything outside the
    reserved fields must be identical, so no other code can have been slipped in.
    """
    if len(candidate) != len(published):
        return False
    try:
        regions = credential_regions(published)
    except ImprintError:
        return False
    return blank_variable_regions(candidate, regions) == blank_variable_regions(published, regions)


def imprinted_ssid(image: bytes, published: bytes) -> str:
    """The network name the firmware in `image` will join.

    Read from where the firmware reads it, using the template to find the spot,
    because imprinting overwrites the marker that would otherwise locate it.
    """
    ssid_at, _ = credential_regions(published)[0]
    return read_c_string(image, ssid_at).decode("utf-8", errors="replace")


def imprinted_password(image: bytes, published: bytes) -> str:
    """The Wi-Fi password the firmware in `image` will use."""
    password_at, _ = credential_regions(published)[1]
    return read_c_string(image, password_at).decode("utf-8", errors="replace")
