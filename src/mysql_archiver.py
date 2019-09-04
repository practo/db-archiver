import gzip
import logging
import os
import sentry_sdk

import archive_utils
import db_utils
import s3_utils
from config_loader import archive_configs, database_config, sentry_dsn
from mysql.connector.errors import ProgrammingError

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(message)s'
)


def start_archival():
    logging.info('Starting archive...')
    for archive_config in archive_configs:
        db_name = database_config.get('database')
        transaction_size = database_config.get('transaction_size')
        archive(archive_config, db_name, transaction_size)


def archive(archive_config, db_name, transaction_size):
    table_name = archive_config.get('table')
    logging.info(
        f'\n\n------------- archiving {db_name}.{table_name} -------------')
    where_clause = archive_config.get('where')
    column_in_file_name = archive_config.get('column_to_add_in_s3_filename')

    archive_db_name = db_name + '_archive'
    archive_table_name = table_name + '_archive'

    db_utils.create_archive_database(db_name, archive_db_name)

    try:
        db_utils.create_archive_table(
            db_name, table_name, archive_db_name, archive_table_name)
    except ProgrammingError as e:
        if e.errno == 1050:
            logging.info(
                f'Archive table {archive_db_name}.{archive_table_name} exists,'
                f' archiving older rows'
            )

            fetch_archived_data_upload_to_s3_and_delete(
                db_name, table_name, archive_db_name, archive_table_name,
                column_in_file_name, transaction_size, '')
            archive(archive_config, db_name, transaction_size)

            return None
        else:
            raise e

    archive_utils.archive_to_db(
        db_name, table_name, archive_db_name, archive_table_name, where_clause,
        transaction_size)

    fetch_archived_data_upload_to_s3_and_delete(
        db_name, table_name, archive_db_name, archive_table_name,
        column_in_file_name, transaction_size, where_clause)


def fetch_archived_data_upload_to_s3_and_delete(
    db_name, table_name, archive_db_name, archive_table_name,
    column_in_file_name, transaction_size, where_clause):
    no_of_rows_archived = db_utils.get_count_of_rows_archived(
        archive_db_name, archive_table_name)
    if not no_of_rows_archived:
        logging.info(
            f'Archive table {archive_db_name}.{archive_table_name} '
            f'had no rows, dropping archive table')
        db_utils.drop_archive_table(archive_db_name, archive_table_name)

        return None

    local_file_name, s3_path = db_utils.get_file_names(
        db_name, table_name, archive_db_name, archive_table_name,
        column_in_file_name, where_clause)

    archive_utils.archive_to_file(
        archive_db_name, archive_table_name, transaction_size, local_file_name)

    gzip_file_name = compress_to_gzip(local_file_name)
    gzip_s3_path = f'{s3_path}.gz'

    # s3_utils.upload_to_s3(local_file_name, s3_path)
    s3_utils.upload_to_s3(gzip_file_name, gzip_s3_path)
    logging.info(f'Deleting local file: {local_file_name}')
    os.remove(local_file_name)
    os.remove(gzip_file_name)

    db_utils.drop_archive_table(archive_db_name, archive_table_name)

    return None


def compress_to_gzip(local_file_name):
    gzip_file_name = f'{local_file_name}.gz'
    fp = open(local_file_name,'rb')
    with gzip.open(gzip_file_name, 'wb') as gz_fp:
        gz_fp.write(bytearray(fp.read()))

    return gzip_file_name


if __name__ == '__main__':
    sentry_sdk.init(dsn=sentry_dsn)
    try:
        start_archival()
    except Exception as e:
        sentry_sdk.capture_exception(e)

        raise e
