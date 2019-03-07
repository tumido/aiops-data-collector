import logging
from io import BytesIO
from threading import Thread, current_thread
from uuid import uuid4

import requests
import prometheus_metrics

import custom_parser
import target_worker

logger = logging.getLogger()
CHUNK = 10240
MAX_RETRIES = 3


def _retryable(method: str, *args, **kwargs) -> requests.Response:
    """Retryable HTTP request.

    Invoke a "method" on "requests.session" with retry logic.
    :param method: "get", "post" etc.
    :param *args: Args for requests (first should be an URL, etc.)
    :param **kwargs: Kwargs for requests
    :return: Response object
    :raises: HTTPError when all requests fail
    """
    thread = current_thread()

    with requests.Session() as session:
        for attempt in range(MAX_RETRIES):
            try:
                resp = getattr(session, method)(*args, **kwargs)

                resp.raise_for_status()
            except (requests.HTTPError, requests.ConnectionError) as e:
                logger.warning(
                    '%s: Request failed (attempt #%d), retrying: %s',
                    thread.name, attempt, str(e)
                )
                continue
            else:
                return resp

    raise requests.HTTPError('All attempts failed')


def query_main_collection(
        host: str,
        endpoint: str,
        main_collection: str,
        query_string: str
) -> dict:
    """Query a Collection.

    Returns a Response object with data retrieved from all pages
    :param host: The host
    :param endpoint: API Endpoint
    :param main_collection: Collection name ('container_nodes', 'sources' etc)
    :param query_string: Query string as additional params to the GET request
    :return: Response object with data retrieved from all pages
    :raises: HTTPError in the function caller
    """
    # First GET call to get data as well as pagination links
    resp = _retryable(
        'get',
        f'{host}{endpoint}/{main_collection}',
        params={query_string: ''},
        verify=False
    )
    out = resp.json()
    all_data = out['data']
    prometheus_metrics.METRICS['get_successes'].inc()

    # Subsequent GET calls that reference the pagination link
    while out['links'].get('next'):
        resp = _retryable(
            'get',
            f'{host}{out["links"]["next"]}',
            verify=False
        )
        out = resp.json()
        all_data += out['data']
        prometheus_metrics.METRICS['get_successes'].inc()
    return all_data


def query_sub_collection(
        host: str,
        endpoint: str,
        main_collection: str,
        sub_collection: str,
        query_string: str,
        collection: list,
        foreign_key: str
) -> dict:
    """Query a SubCollection for all records in the main collection.

    Returns a Response object with data retrieved from all pages
    :param host: The host
    :param endpoint: API Endpoint
    :param main_collection: Collection name ('container_nodes', 'sources' etc)
    :param sub_collection: Sub-Collection name ('tags')
    :param query_string: Query string as additional params to the GET request
    :param collection: List of all main collection items
    :param foreign_key: Foreign key to be added to sub-collection records
    :return: Response object with sub-collection data retrieved from all pages
    :raises: HTTPError in the function caller
    """
    all_data = []
    for item in collection:
        # First GET call to get data as well as pagination links
        resp = _retryable(
            'get',
            f'{host}{endpoint}'
            f'/{main_collection}/{item["id"]}/{sub_collection}',
            params={query_string: ''},
            verify=False
        )
        out = resp.json()

        for row in enumerate(out['data']):
            row[1][foreign_key] = item['id']
        all_data += out['data']
        prometheus_metrics.METRICS['get_successes'].inc()

        # Subsequent GET calls that reference the pagination link
        while out['links'].get('next'):
            resp = _retryable(
                'get',
                f'{host}{out["links"]["next"]}',
                verify=False
            )
            out = resp.json()
            for row in enumerate(out['data']):
                row[1][foreign_key] = item['id']
            all_data += out['data']
            prometheus_metrics.METRICS['get_successes'].inc()
    return all_data


def download_job(
        source_url: str,
        source_id: str,
        dest_url: str,
        b64_identity: str = None
) -> None:
    """Spawn a thread worker for data downloading task.

    Requests the data to be downloaded and pass it to the next service
    :param source_url: Data source location
    :param source_id: Data identifier
    :param dest_url: Location where the collected data should be received
    :param b64_identity: Redhat Identity base64 string
    """
    # When source_id is missing, create our own
    source_id = source_id or str(uuid4())

    def worker_clustering(_clustering_info: dict) -> None:
        """Download, extract data and forward the content."""
        thread = current_thread()
        logger.debug('%s: Worker started', thread.name)

        # Fetch data
        prometheus_metrics.METRICS['gets'].inc()
        try:
            resp = _retryable('get', source_url, stream=True)
        except requests.HTTPError as exception:
            logger.error(
                '%s: Unable to fetch source data for "%s": %s',
                thread.name, source_id, exception
            )
            prometheus_metrics.METRICS['get_errors'].inc()
            return
        prometheus_metrics.METRICS['get_successes'].inc()

        file_obj = BytesIO(resp.content)

        # Store payload ID in a header
        headers = {
            'source_id': source_id,
            'x-rh-identity': b64_identity,
        }

        # Pass to next service
        prometheus_metrics.METRICS['posts'].inc()
        try:
            resp = _retryable(
                'post',
                f'http://{dest_url}',
                data=custom_parser.parse(file_obj),
                headers=headers
            )
            prometheus_metrics.METRICS['post_successes'].inc()
        except requests.HTTPError as exception:
            logger.error(
                '%s: Failed to pass data for "%s": %s',
                thread.name, source_id, exception
            )
            prometheus_metrics.METRICS['post_errors'].inc()

        logger.debug('%s: Done, exiting', thread.name)

    def worker_topology(topology_info: dict) -> None:
        """Download and forward the content."""
        thread = current_thread()
        logger.debug('%s: Worker started', thread.name)

        # Build the POST data object
        data = {
            'id': source_id,
            'data': {}
        }

        host = topology_info["host"]
        endpoint = topology_info["endpoint"]

        for entity in topology_info['queries'].keys():
            prometheus_metrics.METRICS['gets'].inc()

            query_entity = topology_info['queries'][entity]
            query_string = query_entity.get('query_string')
            main_collection = query_entity.get('main_collection')
            sub_collection = query_entity.get('sub_collection')
            foreign_key = query_entity.get('foreign_key')

            if sub_collection:
                try:
                    all_data = query_sub_collection(
                        host,
                        endpoint,
                        main_collection,
                        sub_collection,
                        query_string,
                        data['data'][main_collection],
                        foreign_key
                    )
                except requests.HTTPError as exception:
                    prometheus_metrics.METRICS['get_errors'].inc()
                    logger.error(
                        '%s: Unable to fetch source data for "%s": %s',
                        thread.name, source_id, exception
                    )
                    return
            else:
                try:
                    all_data = query_main_collection(
                        host,
                        endpoint,
                        main_collection,
                        query_string
                    )
                except requests.HTTPError as exception:
                    prometheus_metrics.METRICS['get_errors'].inc()
                    logger.error(
                        '%s: Unable to fetch source data for "%s": %s',
                        thread.name, source_id, exception
                    )
                    return

            data['data'][entity] = all_data

        # Pass to next service
        prometheus_metrics.METRICS['posts'].inc()
        try:
            resp = _retryable(
                'post',
                f'http://{dest_url}',
                json=data,
                headers={"x-rh-identity": b64_identity}
            )
            prometheus_metrics.METRICS['post_successes'].inc()
        except requests.HTTPError as exception:
            logger.error(
                '%s: Failed to pass data for "%s": %s',
                thread.name, source_id, exception
            )
            prometheus_metrics.METRICS['post_errors'].inc()

        logger.debug('%s: Done, exiting', thread.name)

    thread_mappings = {
        'worker_clustering': worker_clustering,
        'worker_topology': worker_topology
    }

    name = target_worker.NAME
    info = target_worker.INFO

    thread = Thread(target=thread_mappings[name](info))
    thread.start()
