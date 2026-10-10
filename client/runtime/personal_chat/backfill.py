"""An opt-in gate until the packaged transport's replay contract is verified."""


def status(config):
    enabled = config.get('backfill_enabled', False)
    if type(enabled) is not bool:
        raise ValueError('QQ_BACKFILL_CONFIG_INVALID')
    return dict(enabled=enabled, status='UNAVAILABLE' if enabled else 'DISABLED',
                error_code='QQ_BACKFILL_UNAVAILABLE' if enabled else None)
