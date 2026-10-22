from django import template
from django.utils.safestring import mark_safe

from .. import accent, dashboard

register = template.Library()


@register.simple_tag(takes_context=True)
def club_accent(context):
    """<style> that sets the accent variables from the current club's colours (a neutral default without a club)."""
    team = context.get("team") or {}
    names = team.get("clubColors") or ""
    hexes = [c["hex"] for c in dashboard.club_colours(names)]
    # accent_css only ever contains #rrggbb values that passed accent.clean_colours, so it is safe to inline
    return mark_safe('<style id="club-accent">\n' + accent.accent_css(hexes) + '\n</style>')
