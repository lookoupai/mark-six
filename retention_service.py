"""Configurable cleanup for time-limited operational data."""

from datetime import datetime, timedelta

from models import BacktestRun, MacauCollectedData, PredictionRecord, StrategyChatMessage, StrategyProposal, SystemConfig, UserNotification, db


RETENTION_CONFIG_DEFAULTS = {
    'prediction_record_retention_days': 365,
    'user_notification_retention_days': 30,
    'backtest_runs_retention_days': 90,
    'macau_collected_data_retention_days': 365,
    'strategy_chat_retention_days': 90,
}


def get_retention_days(config_key):
    """Return a safe retention period (zero means keep forever)."""
    default = RETENTION_CONFIG_DEFAULTS[config_key]
    try:
        value = int(SystemConfig.get_config(config_key, str(default)))
    except (TypeError, ValueError):
        return default
    return min(365, max(0, value))


def cleanup_expired_data(commit=True):
    """Delete expired prediction, notification, backtest, source, and strategy-chat data."""
    now = datetime.now()
    model_keys = {
        'prediction_records': (PredictionRecord, 'prediction_record_retention_days'),
        'user_notifications': (UserNotification, 'user_notification_retention_days'),
        'backtest_runs': (BacktestRun, 'backtest_runs_retention_days'),
        'macau_collected_data': (MacauCollectedData, 'macau_collected_data_retention_days'),
        'strategy_chat_messages': (StrategyChatMessage, 'strategy_chat_retention_days'),
    }
    deleted_counts = {}
    for name, (model, config_key) in model_keys.items():
        retention_days = get_retention_days(config_key)
        if model is BacktestRun and retention_days:
            expired_ids = db.session.query(BacktestRun.id).filter(
                BacktestRun.created_at < now - timedelta(days=retention_days))
            StrategyProposal.query.filter(StrategyProposal.backtest_run_id.in_(expired_ids)).update(
                {"backtest_run_id": None}, synchronize_session=False)
        deleted_counts[name] = 0 if retention_days == 0 else model.query.filter(
            model.created_at < now - timedelta(days=retention_days)
        ).delete(synchronize_session=False)
    if commit and any(deleted_counts.values()):
        db.session.commit()
    return deleted_counts


def cleanup_expired_station_notifications(user_id=None, commit=True):
    """Delete expired in-app notifications, optionally for one user only."""
    retention_days = get_retention_days('user_notification_retention_days')
    if retention_days == 0:
        return 0

    expires_before = datetime.now() - timedelta(days=retention_days)
    query = UserNotification.query.filter(UserNotification.created_at < expires_before)
    if user_id:
        query = query.filter(UserNotification.user_id == user_id)

    deleted = query.delete(synchronize_session=False)
    if deleted and commit:
        db.session.commit()
    return deleted
