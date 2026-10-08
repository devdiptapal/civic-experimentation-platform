"""HTML building blocks, escaped by default.

Every value interpolated into a page goes through :func:`render`, which
escapes it unless it is already :class:`Markup` -- HTML this module built.
Plan text, case records imported from other agencies, and anything a person
typed are all untrusted, and the safe path is the only short one.
"""

from __future__ import annotations

import html as _html
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = [
    "Markup",
    "esc",
    "render",
    "join",
    "chip",
    "state_chip",
    "decision_chip",
    "status_chip",
    "severity_badge",
    "table",
    "field",
    "hidden",
    "button_form",
    "csrf_field",
    "page",
    "STATE_LABELS",
    "DECISION_LABELS",
]


class Markup(str):
    """A string of HTML that has already been made safe.

    Concatenation keeps the result safe: adding a plain string escapes it
    first. Without this, ``Markup + Markup`` would fall back to
    ``str.__add__``, return a plain string, and be escaped a second time
    when interpolated.
    """

    def __html__(self) -> str:  # pragma: no cover - interop nicety
        return self

    def __add__(self, other: Any) -> Markup:
        return Markup(str.__add__(self, esc(other)))

    def __radd__(self, other: Any) -> Markup:
        return Markup(str.__add__(esc(other), self))


def esc(value: Any) -> Markup:
    if isinstance(value, Markup):
        return value
    if value is None:
        return Markup("")
    return Markup(_html.escape(str(value), quote=True))


def render(template: str, **values: Any) -> Markup:
    """``template.format(**values)`` with every value escaped."""
    return Markup(template.format(**{k: esc(v) for k, v in values.items()}))


def join(parts: Iterable[Any], sep: str = "") -> Markup:
    return Markup(sep.join(esc(p) for p in parts))


# ---------------------------------------------------------------------------
# Status: never colour alone -- each chip carries a glyph and a word.
# ---------------------------------------------------------------------------

STATE_LABELS: Mapping[str, tuple[str, str, str]] = {
    # state: (label, glyph, tone)
    "draft": ("Draft", "○", "neutral"),
    "approved": ("Approved", "◇", "info"),
    "running": ("Running", "▶", "good"),
    "paused": ("Paused", "❚❚", "warning"),
    "completed": ("Completed", "✓", "ink"),
    "rolled_back": ("Rolled back", "↺", "critical"),
    "archived": ("Archived", "▣", "neutral"),
}

DECISION_LABELS: Mapping[str, tuple[str, str, str]] = {
    "promote": ("Adopt", "✓", "good"),
    "iterate": ("Revise", "↻", "neutral"),
    "rollback": ("Roll back", "↺", "critical"),
    "inconclusive": ("Inconclusive", "?", "warning"),
    "invalid": ("Invalid", "✕", "critical"),
}

_STATUS = {
    "OK": ("OK", "✓", "good"),
    "WATCH": ("Watch", "▲", "warning"),
    "BREACHED": ("Breached", "!", "critical"),
    "NO DATA": ("No data", "–", "neutral"),
    "helped": ("Helped", "✓", "good"),
    "harmed": ("Harmed", "!", "critical"),
    "no_effect": ("No change", "=", "neutral"),
    "too_few": ("Too few to tell", "…", "neutral"),
    "watch": ("Watch", "▲", "warning"),
    "ok": ("Pass", "✓", "good"),
    "note": ("Note", "i", "info"),
    "warning": ("Warning", "▲", "warning"),
    "invalid": ("Failed", "✕", "critical"),
}


def chip(label: str, glyph: str, tone: str) -> Markup:
    return render(
        '<span class="chip chip-{tone}"><span class="chip-glyph" aria-hidden="true">'
        "{glyph}</span>{label}</span>",
        tone=tone, glyph=glyph, label=label,
    )


def state_chip(state: str | None) -> Markup:
    if state is None:
        return chip("Record invalid", "✕", "critical")
    return chip(*STATE_LABELS.get(state, (state, "•", "neutral")))


def decision_chip(decision: str) -> Markup:
    return chip(*DECISION_LABELS.get(decision, (decision, "•", "neutral")))


def status_chip(status: str) -> Markup:
    return chip(*_STATUS.get(status, (status, "•", "neutral")))


def severity_badge(severity: str) -> Markup:
    glyph, label = {"critical": ("!", "Critical"), "warning": ("▲", "Warning"),
                    "info": ("i", "For your attention")}[severity]
    return render(
        '<span class="sev sev-{s}"><span aria-hidden="true" class="sev-glyph">{g}</span>'
        "{label}</span>",
        s=severity, g=glyph, label=label,
    )


# ---------------------------------------------------------------------------
# Tables and forms
# ---------------------------------------------------------------------------


def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    caption: str = "",
    numeric: Sequence[int] = (),
    empty: str = "Nothing to show yet.",
) -> Markup:
    rows = list(rows)
    if not rows:
        return render('<p class="empty">{e}</p>', e=empty)
    head = join(
        render('<th scope="col"{cls}>{h}</th>',
               cls=Markup(' class="num"') if i in numeric else Markup(""), h=h)
        for i, h in enumerate(headers)
    )
    body = join(
        Markup("<tr>") + join(
            render('<td data-label="{label}"{cls}>{v}</td>',
                   label=headers[i] if i < len(headers) else "",
                   cls=Markup(' class="num"') if i in numeric else Markup(""), v=v)
            for i, v in enumerate(row)
        ) + Markup("</tr>")
        for row in rows
    )
    cap = render("<caption>{c}</caption>", c=caption) if caption else Markup("")
    return render(
        '<div class="table-wrap"><table class="data">{cap}<thead><tr>{head}</tr></thead>'
        "<tbody>{body}</tbody></table></div>",
        cap=cap, head=head, body=body,
    )


def csrf_field(token: str) -> Markup:
    return render('<input type="hidden" name="_csrf" value="{t}">', t=token)


def hidden(name: str, value: Any) -> Markup:
    return render('<input type="hidden" name="{n}" value="{v}">', n=name, v=value)


def field(
    name: str,
    label: str,
    *,
    value: Any = "",
    kind: str = "text",
    help: str = "",
    error: str = "",
    required: bool = False,
    attrs: Mapping[str, Any] | None = None,
    options: Sequence[tuple[str, str]] = (),
    rows: int = 4,
) -> Markup:
    """A labelled form control with its help text and error, linked for screen readers."""
    ident = f"f-{name}"
    described = []
    help_html = Markup("")
    if help:
        described.append(f"{ident}-help")
        help_html = render('<p class="help" id="{i}-help">{h}</p>', i=ident, h=help)
    error_html = Markup("")
    if error:
        described.append(f"{ident}-error")
        error_html = render('<p class="field-error" id="{i}-error">{e}</p>', i=ident, e=error)
    extra = dict(attrs or {})
    if required:
        extra["required"] = True
    if described:
        extra["aria-describedby"] = " ".join(described)
    if error:
        extra["aria-invalid"] = "true"
    attr_html = join(
        render(" {k}", k=k) if v is True else render(' {k}="{v}"', k=k, v=v)
        for k, v in extra.items() if v not in (None, False)
    )
    if kind == "textarea":
        control = render('<textarea id="{i}" name="{n}" rows="{r}"{a}>{v}</textarea>',
                         i=ident, n=name, r=rows, a=attr_html, v=value)
    elif kind == "select":
        opts = join(
            render('<option value="{v}"{sel}>{t}</option>', v=v, t=t,
                   sel=Markup(" selected") if str(v) == str(value) else Markup(""))
            for v, t in options
        )
        control = render('<select id="{i}" name="{n}"{a}>{o}</select>',
                         i=ident, n=name, a=attr_html, o=opts)
    elif kind == "checkbox":
        checked = Markup(" checked") if value else Markup("")
        return render(
            '<div class="field field-check"><input type="checkbox" id="{i}" name="{n}" '
            'value="1"{c}{a}><label for="{i}">{l}</label>{h}{e}</div>',
            i=ident, n=name, c=checked, a=attr_html, l=label, h=help_html, e=error_html,
        )
    else:
        control = render('<input type="{k}" id="{i}" name="{n}" value="{v}"{a}>',
                         k=kind, i=ident, n=name, v=value, a=attr_html)
    return render(
        '<div class="field{err}"><label for="{i}">{l}{req}</label>{h}{e}{c}</div>',
        err=Markup(" has-error") if error else Markup(""), i=ident, l=label,
        req=Markup(' <span class="req">(required)</span>') if required else Markup(""),
        h=help_html, e=error_html, c=control,
    )


def button_form(
    action: str,
    label: str,
    csrf: str,
    *,
    style: str = "secondary",
    fields: Mapping[str, Any] | None = None,
    confirm: str = "",
) -> Markup:
    """A one-button POST form, which is how every state change is made."""
    extras = join(hidden(k, v) for k, v in (fields or {}).items())
    data = render(' data-confirm="{c}"', c=confirm) if confirm else Markup("")
    return render(
        '<form method="post" action="{a}" class="inline-form"{d}>{csrf}{x}'
        '<button type="submit" class="btn btn-{s}">{l}</button></form>',
        a=action, d=data, csrf=csrf_field(csrf), x=extras, s=style, l=label,
    )


# ---------------------------------------------------------------------------
# The page shell
# ---------------------------------------------------------------------------

_NAV = (
    ("/", "Inbox", "inbox"),
    ("/experiments", "Evaluations", "experiments"),
    ("/library", "Evidence library", "library"),
    ("/help", "Guide", "help"),
)


def page(
    title: str,
    body: Markup,
    *,
    user: Any = None,
    workspace_name: str = "",
    jurisdiction: str = "",
    section: str = "",
    flashes: Sequence[tuple[str, str]] = (),
    csrf: str = "",
    version: str = "",
    synthetic: bool = False,
) -> Markup:
    nav_items = list(_NAV)
    if user is not None and user.can("workspace.admin"):
        nav_items.append(("/admin", "Administration", "admin"))
    nav = join(
        render('<li><a href="{h}"{cur}>{t}</a></li>', h=href, t=text,
               cur=Markup(' aria-current="page"') if key == section else Markup(""))
        for href, text, key in nav_items
    ) if user is not None else Markup("")
    account = Markup("")
    if user is not None:
        account = render(
            '<div class="account"><a href="/account">{name}</a>'
            '<form method="post" action="/logout" class="inline-form">{csrf}'
            '<button type="submit" class="link-button">Sign out</button></form></div>',
            name=user.display_name, csrf=csrf_field(csrf),
        )
    flash_html = join(
        render('<div class="flash flash-{k}" role="status">{m}</div>', k=kind, m=message)
        for kind, message in flashes
    )
    banner = Markup(
        '<div class="synthetic-banner" role="note">Demonstration workspace. Every '
        "record here is synthetic; nothing describes a real person or a real pilot.</div>"
    ) if synthetic else Markup("")
    place = render(" · {j}", j=jurisdiction) if jurisdiction else Markup("")
    return render(
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        "<title>{title} · Civic Experimentation Platform</title>"
        '<link rel="stylesheet" href="/static/app.css">'
        '<link rel="icon" href="/static/icon.svg" type="image/svg+xml">'
        '<script src="/static/app.js" defer></script></head>'
        '<body><a class="skip-link" href="#main">Skip to content</a>{banner}'
        '<header class="site-header"><div class="wrap header-inner">'
        '<a class="brand" href="/"><span class="brand-mark" aria-hidden="true">◆</span>'
        '<span><span class="brand-name">Civic Experimentation Platform</span>'
        '<span class="brand-workspace">{ws}{place}</span></span></a>{account}</div>'
        '<nav class="site-nav" aria-label="Main"><div class="wrap"><ul>{nav}</ul></div></nav>'
        "</header>"
        '<main id="main" class="wrap" tabindex="-1">{flash}{body}</main>'
        '<footer class="site-footer"><div class="wrap">'
        "<p>Civic Experimentation Platform {version} · Apache 2.0 · Open source.</p>"
        "<p>This software measures service changes. It makes no eligibility "
        "determination about anyone and is not legal or policy advice.</p>"
        "</div></footer></body></html>",
        title=title, banner=banner, ws=workspace_name, place=place, account=account,
        nav=nav, flash=flash_html, body=body, version=version,
    )
