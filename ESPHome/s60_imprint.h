#pragma once
// Wi-Fi placeholders the conversion tool overwrites in the built image
// (tools/autoflash/imprint.py): the marker, then 33 or 64 bytes of filler.
#include <cstring>
#include <string>

extern "C" __attribute__((used)) const char S60_IMPRINT_SSID[] =
    "S60-IMPRINT-SSID>.................................";
extern "C" __attribute__((used)) const char S60_IMPRINT_PASS[] =
    "S60-IMPRINT-PASS>................................................................";

// Read at run time through a volatile pointer: the compiler knows the
// placeholder text, and would otherwise fold its length or contents in.
inline std::string s60_imprinted(const char *field) {
  const char *volatile at = field;
  return std::string(at);
}

// False while the placeholder is untouched: the image was never imprinted.
inline bool s60_is_imprinted() {
  const char *volatile at = S60_IMPRINT_SSID;
  return at[0] != '\0' && std::strncmp(at, "S60-IMPRINT-", 12) != 0;
}
