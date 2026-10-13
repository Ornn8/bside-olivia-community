"""Restore the existing native desktop mailbox for managed local sessions."""

_WIDGET_GATE = (
    'e.isOfflineMode&&(l.value.mailWidget!==!1&&(l.value.mailWidget=!1),'
    'l.value.musicWidget!==!1&&(l.value.musicWidget=!1))'
)
_LEGACY_WIDGET_GATE = 'l.value.mailWidget=!0,l.value.musicWidget=!0'
_LOGIN_POLL = (
    's.isOfflineMode||(s.appMode===Se.PRO?Lt().proRestoreFromApi():'
    's.appMode===Se.LITE&&(Lt().liteStartPoll(),uo().startPolling()))'
)
_LEGACY_LOGIN_POLL = (
    's.appMode===Se.PRO?Lt().proRestoreFromApi():'
    's.appMode===Se.LITE&&(s.isOfflineMode?uo().startPolling():'
    '(Lt().liteStartPoll(),uo().startPolling()))'
)
_SETTINGS_FILTER = 'const Le=ee.items.filter(tt=>!X.includes(tt.id))'
_CONFIG_INIT = 'B&&A(B),B&&!E.value'
_SETTINGS_WRITE = 'Object.keys(d).length>0&&(Fm(d),'

_HELPERS = '''
function oliviaLocalDesktopMail(state) {
  return state.appMode === Se.LITE && oliviaLocalMailboxRequest({
    url: "/letter/unread_count", baseURL: Te.defaults.baseURL
  });
}
function oliviaRestoreDesktopMail(state) {
  if (!oliviaLocalDesktopMail(state)) return Promise.resolve();
  if (oliviaRestoreDesktopMail.pending) return oliviaRestoreDesktopMail.pending;
  oliviaRestoreDesktopMail.choice = undefined;
  const job = (async () => {
    try {
      const key = "olivia.desktop-mail.v1";
      if (localStorage.getItem(key) === "1") return;
      // Reserve a writable marker before changing the native preference.
      localStorage.setItem(key, "pending");
      let settings = await jm();
      if (typeof settings?.mailWidget !== "boolean" || !oliviaLocalDesktopMail(state)) return;
      for (;;) {
        const wanted = oliviaRestoreDesktopMail.choice ?? true;
        if (settings.mailWidget !== wanted) {
          await Fm({mailWidget: wanted});
          settings = await jm();
        }
        // A user change during the native save takes precedence over migration.
        if (wanted !== (oliviaRestoreDesktopMail.choice ?? true)) continue;
        if (settings?.mailWidget !== wanted || !oliviaLocalDesktopMail(state)) return;
        Jt().updateSettings({mailWidget: wanted}, {syncToClient: false});
        localStorage.setItem(key, "1");
        return;
      }
    } catch (_) {
      // A rejected write/readback remains retryable on the next login.
    }
  })();
  oliviaRestoreDesktopMail.pending = job.finally(() => {
    oliviaRestoreDesktopMail.pending = null;
  });
  return oliviaRestoreDesktopMail.pending;
}
'''


def patch_desktop_mail(source: str) -> str:
    """Keep native polling/settings ownership and retire forced-on patches."""
    if "function oliviaLocalDesktopMail" in source:
        return source
    anchors = (_WIDGET_GATE, _LEGACY_WIDGET_GATE, _LOGIN_POLL,
               _LEGACY_LOGIN_POLL, _SETTINGS_FILTER, _CONFIG_INIT, _SETTINGS_WRITE)
    if not any(anchor in source for anchor in anchors):
        return source
    if any(source.count(anchor) > 1 for anchor in anchors):
        raise ValueError("COMPANION_DESKTOP_MAIL_ANCHOR_INVALID")
    # Normal offline/PRO sessions retain the original restrictions.
    widget_gate = '!oliviaLocalDesktopMail(e)&&(' + _WIDGET_GATE + ')'
    source = source.replace(_WIDGET_GATE, widget_gate, 1)
    source = source.replace(_LEGACY_WIDGET_GATE, widget_gate, 1)
    # Native offline startup selects LITE later; do it before settings watchers run.
    source = source.replace(
        _CONFIG_INIT,
        'B&&A(B),B&&E.value&&oliviaLocalDesktopMail({appMode:Se.LITE})&&'
        '(l.appMode=Se.LITE),B&&!E.value',
        1,
    )
    source = source.replace(
        _SETTINGS_WRITE,
        'Object.keys(d).length>0&&(oliviaRestoreDesktopMail.pending&&'
        'typeof d.mailWidget==="boolean"&&(oliviaRestoreDesktopMail.choice=d.mailWidget),Fm(d),',
        1,
    )
    source = source.replace(
        _SETTINGS_FILTER,
        'const Le=ee.items.filter(tt=>!X.includes(tt.id)||'
        '(tt.id==="mail-widget"&&oliviaLocalDesktopMail(Ie())))',
        1,
    )
    # startPolling owns its existing single interval; opening the inbox reuses it.
    login_poll = '(oliviaLocalDesktopMail(s)&&(uo().startPolling(),oliviaRestoreDesktopMail(s)),' + _LOGIN_POLL + ')'
    source = source.replace(_LOGIN_POLL, login_poll, 1)
    source = source.replace(_LEGACY_LOGIN_POLL, login_poll, 1)
    return _HELPERS + source
