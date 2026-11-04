"""A converted plug's own locks - Tasmota's web password, ESPHome's update
password, web login and API key - asked for when the plug wants them, held
in memory only.
"""

from __future__ import annotations

import functools
import time
from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

from tools.autoflash import detect, device, esphome, esphome_ota, noise, output, prompts, runs
from tools.autoflash.device import NeedsPassword
from tools.autoflash.errors import Abort

if TYPE_CHECKING:
    from tools.autoflash.context import Context


# Found on the network, but saying whether it is a plug this tool converts
# only once given its web password (Tasmota) or web login (ESPHome).
LOCKED = ("locked", "esphome-login")


def identify(ctx: Context, host: str) -> str | None:
    """detect.identify, with the plug's web password and web login, where known."""
    return detect.identify(host, password=ctx.web_password, esphome_login=ctx.esphome_login)


def locked_out(ctx: Context, plug: detect.Found) -> str:
    """Why an unattended run stops at `plug`, locked: what to give."""
    if plug.firmware == "esphome-login":
        return (f"ESPHome on the device at {plug.host} has a web login"
                f"{', and the one given is not it' if ctx.esphome_login else ''}: {ESPHOME_LOGIN_HINT}")
    return str(NeedsPassword(plug.host, bool(ctx.web_password)))


@dataclass
class Secret:
    """A secret a plug can be given: what it is called, ask() to have it
    typed (kept out of the log), and check(value): False once the plug takes
    it, True if it does not, None if it did not answer - or a ValueError
    saying why it is not one at all (a key that is not base64, say)."""
    kind: str
    ask: Callable[[], Any]
    check: Callable[[Any], bool | None]


def typed_secret(prompt: str, label: str) -> Callable[[], str]:
    def ask() -> str:
        value = prompts.ask_secret(f"{prompt}: ")
        output.private(value, label)
        return value
    return ask


# A check the plug gave no answer to is asked again this many times, this far
# apart, before the person is: a plug busy or slow to answer one moment
# answers the next, and asking again is all they could choose.
NO_ANSWER_TRIES = 3
NO_ANSWER_PAUSE = 5.0


def answered(check: Callable[[], bool | None]) -> bool | None:
    """check(), asked again while the plug gives no answer (None), up to
    NO_ANSWER_TRIES times more."""
    answer = check()
    for _ in range(NO_ANSWER_TRIES):
        if answer is not None:
            break
        output.say(f"    The device did not answer; asking it again in {NO_ANSWER_PAUSE:.0f}s.")
        time.sleep(NO_ANSWER_PAUSE)
        answer = check()
    return answer


def secret_or_retry(title: str, lines: list[str], secrets: list[Secret],
                    retry: Callable[[], bool | None], *, still: str,
                    last: str | None = None) -> tuple[Secret | None, Any]:
    """What the plug takes now: one of `secrets`, typed - or, the last
    choice, none, once its owner has removed what it wanted, `retry()` saying
    as a check does whether it takes that (`still`, while it does not).
    `last`: that choice in other words.  Asked until it takes one, or the run
    is stopped.  Returns the secret and its value, or (None, None)."""
    output.transient_begin()
    prompts.section(title, lines)
    options = [f"Type its {secret.kind}" for secret in secrets]
    options.append(last or f"Try again without {'one' if secrets else 'it'} - once you have removed it yourself")
    while True:
        choice = prompts.ask_choice("What now", options)
        secret = secrets[choice] if choice < len(secrets) else None
        try:
            if secret is None:
                value, answer = None, answered(retry)
            else:
                value = secret.ask()
                answer = answered(functools.partial(secret.check, value)) if value else True
        except ValueError as exc:
            output.tell(f"    {exc}.")
            continue
        if answer is False:
            output.transient_end()
            return secret, value
        output.tell("    The device did not answer; choose again to try once more." if answer is None
             else f"    That is not its {secret.kind}." if secret else f"    {still}")


def password_or_retry(title: str, lines: list[str], check: Callable[[str | None], bool | None], *,
                      still: str, kind: str | None = None, prompt: str = "", label: str = "") -> str | None:
    """secret_or_retry for one password, its `kind` (asked as `prompt`, kept
    out of the log as `label`), or none: `check(None)` for trying again."""
    secrets = [Secret(kind, typed_secret(prompt, label), check)] if kind else []
    return secret_or_retry(title, lines, secrets, lambda: check(None), still=still)[1]


def ask_web_password(ctx: Context, host: str) -> None:
    """The web password of the Tasmota at `host`, which wants one this run
    does not have: typed, each try checked on the plug, or none once it has
    been cleared on the plug.  Held in memory for this run only, as the Wi-Fi
    password is."""
    wrong = bool(ctx.web_password)
    if prompts.UNATTENDED:
        raise Abort(str(NeedsPassword(host, wrong)))
    ctx.web_password = password_or_retry("Tasmota's web password", [
        f"Tasmota on the device at {host} has a web password.",
        *(["The one given (--web-password, or AUTOFLASH_TASMOTA_PASSWORD) is not it."] if wrong else []),
        "Type it to go on (Tasmota's one user name, admin, is filled in); or clear it",
        "in Tasmota yourself - Configuration > Other > Web Admin Password, emptied",
        "and saved, or the console command WebPassword 0 - and try again without one.",
        "A password typed is held in memory for this run only, never written to disk.",
    ], lambda password: device.Device(host, password=password).wants_password(),
        still="Tasmota there still has a web password.",
        kind="web password", prompt="Tasmota web password", label="tasmota-password")


ESPHOME_PASSWORD_HINT = ("give it with --esphome-ota-password, or as "
                         "AUTOFLASH_ESPHOME_OTA_PASSWORD in .env or the environment")
ESPHOME_KEY_HINT = ("give it with --esphome-api-key, or as "
                    "AUTOFLASH_ESPHOME_API_KEY in .env or the environment")
ESPHOME_LOGIN_HINT = ("give it with --esphome-web-user and --esphome-web-password, or as "
                      "AUTOFLASH_ESPHOME_WEB_USER and AUTOFLASH_ESPHOME_WEB_PASSWORD in .env "
                      "or the environment")


def esphome_key_secret(check: Callable[[bytes], bool | None]) -> Secret:
    """ESPHome's API encryption key, typed as its YAML config has it and
    `check`ed once it is one."""
    def checked(text: str) -> bool | None:
        try:
            key = noise.decode_key(text)
        except ValueError as exc:
            raise ValueError(f"That is not an API encryption key ({exc}): one is 32 bytes in base64, "
                             "as after key: under api: encryption: in the YAML config") from None
        return check(key)
    return Secret("API encryption key", typed_secret("ESPHome API encryption key", "esphome-api-key"),
                  checked)


def api_key_check(host: str, key: bytes) -> bool | None:
    """For secret_or_retry: whether ESPHome's API at `host` does not take `key`."""
    access = esphome.api_access(host, key)
    return None if access is None else access == esphome.API_KEY


def esphome_update_check(host: str, password: str | None, key: bytes | None) -> bool | None:
    """For secret_or_retry: whether ESPHome's updates at `host` still want
    more than `password` and `key`."""
    wants = esphome_ota.wants(host, password, key)
    return None if wants is None else wants != esphome_ota.NOTHING


def ask_esphome_password(ctx: Context, host: str) -> None:
    """The update password of ESPHome at `host`, which wants one this run does
    not have: typed, each try checked on the plug, or none once its owner has
    removed it - or, from an ESPHome that takes encrypted updates too, its
    API encryption key, with which it asks for no password.  Held in memory
    for this run only."""
    wrong = bool(ctx.esphome_password)
    if prompts.UNATTENDED:
        raise Abort(f"{esphome_ota.NeedsPassword(wrong)}: {ESPHOME_PASSWORD_HINT}")
    offer = esphome_ota.probe(host)
    keyed = bool(offer and offer.encryption)
    secrets = [Secret("update password", typed_secret("ESPHome update password", "esphome-password"),
                      lambda password: esphome_update_check(host, password, None))]
    if keyed:
        secrets.append(esphome_key_secret(lambda key: esphome_update_check(host, None, key)))
    secret, value = secret_or_retry("ESPHome's update password", [
        f"ESPHome on the device at {host} has an update password (set in Home",
        "Assistant, say). The next firmware goes on as an ESPHome update, which needs",
        "it. Type it to go on; or remove it from the device's ESPHome YAML config (the",
        "'password:' line under 'ota:'), re-flash, then try again without one.",
        *(["Its API encryption key does instead (key: under api: encryption: there):",
           "with it, the update goes encrypted."] if keyed else []),
        *(["The one given is not it."] if wrong else []),
        "What you type is held in memory for this run only, never written to disk.",
    ], secrets, lambda: esphome_update_check(host, None, None),
        still="ESPHome there still has an update password.")
    if secret is not None and secret.kind == "API encryption key":
        ctx.esphome_key = noise.decode_key(value)
    else:
        ctx.esphome_password = value


def ask_esphome_key(ctx: Context, host: str, *, wifi: bool = False) -> bool:
    """The API encryption key of ESPHome at `host`, typed, each try checked
    on the plug: for its updates, when it takes only encrypted ones - or none,
    once its owner has removed that - or, `wifi`, to unlock its API, which then
    gives its Wi-Fi details (they are typed otherwise).  Held in memory for
    this run only.  Returns whether it was given."""
    wrong = ctx.esphome_key is not None
    if prompts.UNATTENDED:
        if wifi:
            return False  # the Wi-Fi is asked for next, which stops the run
        raise Abort(f"{esphome_ota.WrongKey() if wrong else esphome_ota.NeedsEncryption()}: "
                    f"{ESPHOME_KEY_HINT}; or remove that encryption: line, re-flash, then run this again")
    if wifi:
        secret, value = secret_or_retry("ESPHome's API encryption key", [
            f"ESPHome on the device at {host} gives its Wi-Fi details through its API (ours",
            "does from 1.1.0), but its API is encrypted (api: encryption: in its ESPHome",
            "YAML config). Type its key - after key: there - and it gives them; or type",
            "the Wi-Fi details yourself instead.",
            "A key typed is held in memory for this run only, never written to disk.",
        ], [esphome_key_secret(lambda key: api_key_check(host, key))],
            lambda: False, still="", last="Type the Wi-Fi details instead")
    else:
        secret, value = secret_or_retry("ESPHome's encrypted updates", [
            f"ESPHome on the device at {host} takes only encrypted updates: the ota:",
            "section of its ESPHome YAML config has encryption:, which makes them with its",
            "API encryption key (after key: under api: encryption: there). Type that key",
            "to go on; or remove encryption: from under ota:, re-flash, then try again.",
            *(["The one given (--esphome-api-key, or AUTOFLASH_ESPHOME_API_KEY) is not it."]
              if wrong else []),
            "A key typed is held in memory for this run only, never written to disk.",
        ], [esphome_key_secret(lambda key: esphome_update_check(host, ctx.esphome_password, key))],
            lambda: wants_key(host, ctx.esphome_password),
            still="ESPHome there still takes only encrypted updates.")
    ctx.esphome_key = noise.decode_key(value) if secret is not None else None
    if not wifi and secret is None and esphome_update_check(host, ctx.esphome_password, None) is True:
        ask_esphome_password(ctx, host)  # encryption: gone, an update password put instead
    return ctx.esphome_key is not None


def wants_key(host: str, password: str | None) -> bool | None:
    """Whether ESPHome's updates at `host` still want a key (they take only
    encrypted ones); None when it does not answer."""
    wants = esphome_ota.wants(host, password, None)
    return None if wants is None else wants == esphome_ota.KEY


def ask_esphome_login(ctx: Context, host: str) -> None:
    """The web login of ESPHome at `host` (web_server's auth:), which wants
    one this run does not have: typed, each try checked on the plug, or none
    once its owner has removed it.  Held in memory for this run only."""
    wrong = ctx.esphome_login is not None
    if prompts.UNATTENDED:
        raise Abort(locked_out(ctx, detect.Found(host, "esphome-login")))

    def ask() -> tuple[str, str]:
        user = prompts.ask_text("ESPHome web user name")
        password = prompts.ask_secret("ESPHome web password: ")
        output.private(password, "esphome-web-password")
        return user, password

    def check(login: tuple[str, str] | None) -> bool | None:
        if login:
            esphome.use_login(host, *login)
        found = esphome.page(host)
        return None if found is None else found == "login"

    _, ctx.esphome_login = secret_or_retry("ESPHome's web login", [
        f"ESPHome on the device at {host} has a web login (auth: under web_server: in",
        "its ESPHome YAML config). Type its user name and password to go on; or remove",
        "auth: from there, re-flash, then try again without one.",
        *(["The one given (--esphome-web-user and --esphome-web-password, or as",
           "AUTOFLASH_ESPHOME_WEB_USER and AUTOFLASH_ESPHOME_WEB_PASSWORD) is not it."]
          if wrong else []),
        "A password typed is held in memory for this run only, never written to disk.",
    ], [Secret("web login", ask, check)], lambda: check(None),
        still="ESPHome there still has a web login.")


def check_esphome_key(ctx: Context, host: str) -> None:
    """An API encryption key given in advance, checked on the plug's API:
    asked for again, or gone on without, when it is not the plug's.  One
    given for a plug whose API is plain is not needed, and left unused."""
    if ctx.esphome_key is None:
        return
    access = esphome.api_access(host, ctx.esphome_key)
    if access == esphome.API_PLAIN:
        output.say("    ESPHome's API on the device is not encrypted: the API encryption key given is not needed.")
        ctx.esphome_key = None
    elif access == esphome.API_KEY:
        if prompts.UNATTENDED:
            raise Abort(f"{esphome_ota.WrongKey()}: {ESPHOME_KEY_HINT}")
        secret, value = secret_or_retry("ESPHome's API encryption key", [
            "The API encryption key given (--esphome-api-key, or AUTOFLASH_ESPHOME_API_KEY)",
            f"is not the one ESPHome on the device at {host} has: its YAML config has it, after",
            "key: under api: encryption:. Type that one; or go on without it, when the",
            "device's Wi-Fi details and its update can be had another way.",
            "A key typed is held in memory for this run only, never written to disk.",
        ], [esphome_key_secret(lambda key: api_key_check(host, key))],
            lambda: False, still="", last="Go on without it")
        ctx.esphome_key = noise.decode_key(value) if secret is not None else None


def unlock_esphome(ctx: Context) -> None:
    """What ESPHome on the plug wants, known before the go-ahead - so that
    nothing is asked once the plug is being changed: an API encryption key
    given, checked; then for its updates, its update password, when it has
    one (or its key, which does instead where it takes encrypted updates
    too), or its key, when it takes only encrypted updates.  Each given
    (--esphome-ota-password, --esphome-api-key, or their AUTOFLASH_ names), or
    asked for."""
    host = ctx.state.input("plug_ip")
    if not host:
        return
    check_esphome_key(ctx, host)
    wants = esphome_ota.wants(host, ctx.esphome_password, ctx.esphome_key)
    if wants == esphome_ota.KEY:
        ask_esphome_key(ctx, host)
    elif wants == esphome_ota.PASSWORD:
        ask_esphome_password(ctx, host)


def unlocked(ctx: Context, plug: detect.Found) -> detect.Found:
    """`plug`, a Tasmota with a web password or an ESPHome with a web login,
    as what it is once given it."""
    if plug.firmware == "esphome-login":
        ask_esphome_login(ctx, plug.host)
        if identify(ctx, plug.host) != "esphome":
            raise Abort(f"the ESPHome at {plug.host} did not answer as a device this tool converts")
        return detect.Found(plug.host, "esphome", plug.mac)
    ask_web_password(ctx, plug.host)
    running = identify(ctx, plug.host)
    if running in ("mover", "startup", "hub"):
        running = "midway"
    if running not in ("tasmota", "midway"):
        raise Abort(f"the Tasmota at {plug.host} did not answer as a device this tool converts")
    return detect.Found(plug.host, running, plug.mac)


def unlock_tasmota(ctx: Context) -> None:
    """The plug's own Tasmota web password, when it has one, known before any
    step asks the plug for anything: on a run from Tasmota, until the plug
    leaves it (L1) - every firmware after that is one this tool put on.  A
    run carried on was not asked yet, nor one given the wrong password."""
    source = runs.run_source(ctx.state)
    if source == "outdated" and runs.run_firmware(ctx.state) == "tasmota":
        source = "tasmota"   # updated: an official Tasmota, its password its own
    if source not in ("tasmota", "safeboot", "untemplated") or ctx.state.is_done("L1"):
        return
    if source == "safeboot" and not ctx.state.is_done("T1"):
        return  # its safeboot first: T1 asks for it there
    host = ctx.state.input("plug_ip")
    if host and ctx.device.wants_password() is True:
        ask_web_password(ctx, host)
