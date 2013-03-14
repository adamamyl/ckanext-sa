import json
import requests
import datetime
import itertools
import messytables
from messytables import (AnyTableSet, types_processor,
                         headers_guess, headers_processor, headers_make_unique,
                         type_guess, offset_processor)
from pylons import config
from ckan.lib.cli import CkanCommand
import ckan.logic as logic
import ckan.model as model
from fetch_resource import download

import logging
logger = logging.getLogger()

TYPE_MAPPING = {
    messytables.types.StringType: 'text',
    messytables.types.IntegerType: 'numeric',  # 'int' may not be big enough,
                    # and type detection may not realize it needs to be big
    messytables.types.FloatType: 'float',
    messytables.types.DecimalType: 'numeric',
    messytables.types.DateType: 'timestamp',
    messytables.types.DateUtilType: 'timestamp'
}

class DatastorerException(Exception):
    pass


class DataStore(CkanCommand):
    """
    Upload all resources from the FileStore to the DataStore

    Usage:

    paster datastore [package-id]
            - Update all resources or just those belonging to a specific
              package if a package id is provided.
    """
    summary = __doc__.split('\n')[0]
    usage = __doc__
    min_args = 0
    max_args = 1
    MAX_PER_PAGE = 50
    max_content_length = int(config.get('ckanext-archiver.max_content_length',
        50000000))

    DATA_FORMATS = [
        'csv',
        'tsv',
        'text/csv',
        'txt',
        'text/plain',
        'text/tsv',
        'text/tab-separated-values',
        'xls',
        'application/ms-excel',
        'application/vnd.ms-excel',
        'application/xls',
        'application/octet-stream',
        'text/comma-separated-values',
        'application/x-zip-compressed',
        'application/zip',
    ]

    def _get_all_packages(self):
        page = 1
        context = {
            'model': model,
        }
        while True:
            data_dict = {
                'page': page,
                'limit': self.MAX_PER_PAGE,
            }
            packages = logic.get_action('current_package_list_with_resources')(
                                        context, data_dict)
            if not packages:
                raise StopIteration
            for package in packages:
                yield package
            page += 1

    def command(self):
        """
        Parse command line arguments and call the appropriate method
        """
        if self.args and self.args[0] in ['--help', '-h', 'help']:
            print Datastore.__doc__
            return

        if self.args:
            cmd = self.args[0]
        self._load_config()
        user = logic.get_action('get_site_user')({'model': model,
                                            'ignore_auth': True}, {})
        packages = self._get_all_packages()
        context = {
            'site_url': config['ckan.site_url'],
            'apikey': user.get('apikey'),
            'site_user_apikey': user.get('apikey'),
            'username': user.get('name'),
            'webstore_url': config.get('ckan.webstore_url')
        }
        for package in packages:
            for resource in package.get('resources', []):
                mimetype = resource['mimetype']
                if mimetype and (mimetype not in self.DATA_FORMATS
                        or resource['format'].lower() not in
                        self.DATA_FORMATS):
                    logger.warn('Skipping resource {0} from package {1} '
                            'because MIME type {2} or format {3} is '
                            'unrecognized'.format(resource['url'],
                            package['name'], mimetype, resource['format']))
                    continue
                logger.info('Datastore resource from resource {0} from '
                            'package {0}'.format(resource['url'],
                                                 package['name']))
                self.push_to_datastore(context, resource)
                break
            break


    def push_to_datastore(self, context, resource):
        result  = download(context, resource,
                self.max_content_length, self.DATA_FORMATS)
        content_type = result['headers'].get('content-type', '')\
                                        .split(';', 1)[0]  # remove parameters

        f = open(result['saved_file'], 'rb')
        table_sets = AnyTableSet.from_fileobj(f, mimetype=content_type, extension=resource['format'].lower())

        ##only first sheet in xls for time being
        row_set = table_sets.tables[0]
        offset, headers = headers_guess(row_set.sample)
        row_set.register_processor(headers_processor(headers))
        row_set.register_processor(offset_processor(offset + 1))
        row_set.register_processor(datetime_procesor())

        logger.info('Header offset: {0}.'.format(offset))

        guessed_types = type_guess(
            row_set.sample,
            [
                messytables.types.StringType,
                messytables.types.IntegerType,
                messytables.types.FloatType,
                messytables.types.DecimalType,
                messytables.types.DateUtilType
            ],
            strict=True
        )
        logger.info('Guessed types: {0}'.format(guessed_types))
        row_set.register_processor(types_processor(guessed_types, strict=True))
        row_set.register_processor(stringify_processor())

        ckan_url = context['site_url'].rstrip('/')

        datastore_create_request_url = '%s/api/3/action/datastore_create' % (ckan_url)

        guessed_type_names = [TYPE_MAPPING[type(gt)] for gt in guessed_types]

        def send_request(data):
            request = {'resource_id': resource['id'],
                       'fields': [dict(id=name, type=typename) for name, typename in zip(headers, guessed_type_names)],
                       'records': data}
            response = requests.post(datastore_create_request_url,
                             data=json.dumps(request),
                             headers={'Content-Type': 'application/json',
                                      'Authorization': context['apikey']},
                             )
            try:
                if not response.status_code:
                    raise DatastorerException('Datastore is not reponding at %s with '
                            'response %s' % (datastore_create_request_url, response))
            except Exception:
                pass
                #TODO: Put retry code here
            if response.status_code not in (201, 200):
                logger.error('Response was {0}'.format(self.get_response_error(response)))
                raise DatastorerException('Datastorer bad response code (%s) on %s. Response was %s' %
                        (response.status_code, datastore_create_request_url, response))

        # Delete any existing data before proceeding. Otherwise 'datastore_create' will
        # append to the existing datastore. And if the fields have significantly changed,
        # it may also fail.
        try:
            logger.info('Deleting existing datastore (it may not exist): {0}.'.format(resource['id']))
            response = requests.post('%s/api/3/action/datastore_delete' % (ckan_url),
                            data=json.dumps({'resource_id': resource['id']}),
                            headers={'Content-Type': 'application/json',
                                    'Authorization': context['apikey']}
                            )
            if not response.status_code or response.status_code not in (200, 404):
                # skips 200 (OK) or 404 (datastore does not exist, no need to delete it)
                logger.error('Deleting existing datastore failed: {0}'.format(self.get_response_error(response)))
                raise DatastorerException("Deleting existing datastore failed.")
        except requests.exceptions.RequestException as e:
            logger.error('Deleting existing datastore failed: {0}'.format(str(e)))
            raise DatastorerException("Deleting existing datastore failed.")

        logger.info('Creating: {0}.'.format(resource['id']))

        # generates chunks of data that can be loaded into ckan
        # n is the maximum size of a chunk
        def chunky(iterable, n):
            it = iter(iterable)
            while True:
                chunk = list(
                    itertools.imap(
                        dict, itertools.islice(it, n)))
                if not chunk:
                    return
                yield chunk

        count = 0
        for data in chunky(row_set.dicts(), 100):
            count += len(data)
            send_request(data)

        logger.info("There should be {n} entries in {res_id}.".format(n=count, res_id=resource['id']))

        ckan_request_url = ckan_url + '/api/3/action/resource_update'

        resource.update({
            'webstore_url': 'active',
            'webstore_last_updated': datetime.datetime.now().isoformat()
        })

        response = requests.post(
            ckan_request_url,
            data=json.dumps(resource),
            headers={'Content-Type': 'application/json',
                     'Authorization': context['apikey']})

        if response.status_code not in (201, 200):
            raise DatastorerException('Ckan bad response code (%s). Response was %s' %
                                 (response.status_code, response.content))


    def get_response_error(self, response):
        if not response.content:
            return repr(response)
        try:
            d = json.loads(response.content)
        except ValueError:
            return repr(response) + " <" + response.content + ">"
        if "error" in d:
            d = d["error"]
        return repr(response) + "\n" + json.dumps(d, sort_keys=True, indent=4) + "\n"


def stringify_processor():
    def to_string(row_set, row):
        for cell in row:
            if not cell.value:
                cell.value = None
            else:
                cell.value = unicode(cell.value)
        return row
    return to_string


def datetime_procesor():
    ''' Stringifies dates so that they can be parsed by the db
    '''
    def datetime_convert(row_set, row):
        for cell in row:
            if isinstance(cell.value, datetime.datetime):
                cell.value = cell.value.isoformat()
                cell.type = messytables.StringType()
        return row
    return datetime_convert
