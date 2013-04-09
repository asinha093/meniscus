
from datetime import datetime, timedelta

from oslo.config import cfg
from meniscus.api.personalities import PERSONALITIES
from meniscus.config import get_config
from meniscus.config import init_config
from meniscus.data.model.worker import WatchlistItem
from meniscus.api.coordinator import coordinator_flow

# cache configuration options
_WATCHLIST_GROUP = cfg.OptGroup(name='watchlist_settings',
                                title='Watchlist Settings')
get_config().register_group(_WATCHLIST_GROUP)

_WATCHLIST_THRESHOLDS = [
    cfg.IntOpt('failure_tolerance_seconds',
               default=60,
               help="""default duration for monitoring failed workers"""
               ),
    cfg.IntOpt('watchlist_count_threshold',
               default=5,
               help="""count of reported failures"""
               )
]

get_config().register_opts(_WATCHLIST_THRESHOLDS, group=_WATCHLIST_GROUP)
try:
    init_config()
    conf = get_config()
except cfg.ConfigFilesNotFoundError:
    conf = get_config()

FAILURE_TOLERANCE_SECONDS = conf.watchlist_settings.failure_tolerance_seconds
WATCHLIST_COUNT_THRESHOLD = conf.watchlist_settings.watchlist_count_threshold


def _add_watchlist_item(db, worker_id):
    """
    adds item to watchlist
    """
    watch_item = WatchlistItem(worker_id)
    db.put('watchlist', watch_item.format())


def _update_watchlist_item(db, watch_dict):
    """
    updates watch count and last changed for watchlist item
    """
    updated_worker = WatchlistItem(watch_dict['worker_id'],
                                   datetime.now(),
                                   watch_dict['watch_count'] + 1,
                                   watch_dict['_id'])
    db.update('watchlist', updated_worker.format_for_save())


def _delete_expired_watchlist_items(db):
    """
    deletes all expired watchlist items when method is called with respect to
    the threshold constant
    """
    threshold = datetime.now() - timedelta(seconds=FAILURE_TOLERANCE_SECONDS)

    # flush out all expired workers
    db.delete('watchlist', {'last_changed': {'$lt': threshold}})


def broadcast_config_change(db, worker):
    """
    Broadcast configuration change to all workers upstream from
    failed offline worker
    """

    upstream_list = [p['personality'] for p in PERSONALITIES
                     if p['downstream'] == worker.personality
                     or p['alternate'] == worker.personality]
    upstream_workers = db.find(
        'worker', {'personality': {'$in': upstream_list}})

    #todo: remove return and send to broadcast worker
    # return [Worker(**worker).get_pipeline_info()
    #         for worker in upstream_workers]
    print "pipeline sent to broadcaster"


def process_watchlist_item(db, worker_id):
    """
    process a worker id with respect to the watchlist table.
    delete expired watchlist items
    add or update watchlist entry
    broadcast new worker configuration if watch_count is met
    """

    _delete_expired_watchlist_items(db)

    watch_dict = db.find_one('watchlist', {'worker_id': worker_id})
    if not watch_dict:
        _add_watchlist_item(db, worker_id)
    else:
        # update watchlist entry
        if watch_dict['watch_count'] == WATCHLIST_COUNT_THRESHOLD:
            worker = coordinator_flow.find_worker(db, worker_id)

            if worker.status != 'offline':
                coordinator_flow.update_worker_status(db, worker, 'offline')
                broadcast_config_change(db, worker)

        _update_watchlist_item(db, watch_dict)
