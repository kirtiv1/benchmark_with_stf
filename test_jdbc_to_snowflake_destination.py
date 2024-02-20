# Copyright 2022 StreamSets Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import string

import pytest
from streamsets.sdk.utils import Version
from streamsets.testframework.markers import database, sdc_min_version, snowflake
from streamsets.testframework.utils import get_random_string

logger = logging.getLogger(__name__)

# AWS S3 bucket in case of AWS or Azure blob storage container in case of Azure.
STORAGE_BUCKET_CONTAINER = 'snowflake'

NEW_STAGE_LOCATIONS = {'AZURE': 'BLOB_STORAGE'}

@pytest.fixture(scope='module')
def sdc_common_hook():
    def hook(data_collector):
        data_collector.SDC_JAVA_OPTS = '-Xmx8192m -Xms8192m'
        data_collector.sdc_properties['production.maxBatchSize'] = '100000'
    return hook

def get_stage_location(sdc_builder, stage_location):
    if Version(sdc_builder.version) < Version("5.7.0"):
        return stage_location
    else:
        new_stage_location = NEW_STAGE_LOCATIONS.get(stage_location)
        return new_stage_location if new_stage_location else stage_location

@sdc_min_version('2.7.0.0')
@database
#@snowflake
#@pytest.mark.parametrize('number_of_threads_and_tables', [1, 2, 4, 8])
@pytest.mark.parametrize('number_of_threads_and_tables', [4, 8])
@pytest.mark.parametrize('batch_size', [20_000, 100_000])
#@pytest.mark.parametrize('batch_size', [20_000])
#@pytest.mark.parametrize('stage_location', ["AWS_S3", "AZURE", "GCS"])
@pytest.mark.parametrize('stage_location', ["AWS_S3"])
def test_multithreaded_batch_sizes(sdc_builder, sdc_executor, database, origin_table, snowflake, number_of_threads_and_tables, batch_size, stage_location):
    """Benchmark Snowflake destination with different thread and batch size combinations"""

    #if snowflake.sdc_stage_configurations['com_streamsets_pipeline_stage_destination_snowflake_SnowflakeDTarget'][
    #        'config.stageLocation'] == 'INTERNAL':
    #    pytest.skip('This test is specific to Snowflake external staging')

    #number_of_records = 2_000_000
    number_of_records = 2_000_0

    # JDBC
    partition_size = str(int(number_of_records / number_of_threads_and_tables))

    # Snowflake
    random_table_suffix = get_random_string(string.ascii_uppercase, 5)
    table_names = [f'STF_TABLE_{idx}_{random_table_suffix}' for idx in range(number_of_threads_and_tables)]
    stage_name = f'STF_STAGE_{get_random_string(string.ascii_uppercase, 5)}'

    # Connect to the tables (tables are created by the pipeline).
    tables = [snowflake.describe_table(table_name) for table_name in table_names]
    # The following is a path inside a bucket in the case of AWS S3 or
    # a path inside a container in the case of Azure Blob Storage container.
    storage_path = f'{STORAGE_BUCKET_CONTAINER}/{get_random_string(string.ascii_letters, 10)}'
    snowflake.create_stage(stage_name, storage_path, get_stage_location(sdc_builder, stage_location))

    # Start building the pipeline
    pipeline_builder = sdc_builder.get_pipeline_builder()

    snowflake_destination = pipeline_builder.add_stage('Snowflake', type='destination')
    snowflake_destination.set_attributes(purge_stage_file_after_ingesting=True,
                                         snowflake_stage_name=stage_name,
                                         data_drift_enabled=True,
                                         ignore_missing_fields=True,
                                         table_auto_create=True,
                                         table="STF_TABLE_${record:value('/ID') % " + str(number_of_threads_and_tables)
                                               + '}_' + random_table_suffix)

    if Version(sdc_builder.version) < Version("5.7.0"):
        snowflake_destination.set_attributes(connection_pool_size=number_of_threads_and_tables)
    else:
        snowflake_destination.set_attributes(maximum_connection_threads=number_of_threads_and_tables)

    jdbc_multitable_consumer = pipeline_builder.add_stage('JDBC Multitable Consumer')
    jdbc_multitable_consumer.set_attributes(table_configs=[dict(tablePattern=origin_table.name,
                                                                partitioningMode='BEST_EFFORT',
                                                                maxNumActivePartitions=-1,
                                                                partitionSize=partition_size)],
                                            number_of_threads=number_of_threads_and_tables,
                                            maximum_pool_size=number_of_threads_and_tables)

    jdbc_multitable_consumer >> snowflake_destination

    pipeline = pipeline_builder.build().configure_for_environment(snowflake, database)

    # Load data in JDBC table
    origin_table.load_records(number_of_records)

    # Run the pipeline for benchmarking
    engine = snowflake.engine
    try:
        sdc_executor.benchmark_pipeline(pipeline, record_count=number_of_records)
    finally:
        logger.debug('Staged files will be deleted from %s ...', storage_path)
        snowflake.delete_staged_files(storage_path)
        logger.debug('Dropping Snowflake stage %s ...', stage_name)
        snowflake.drop_entities(stage_name=stage_name)
        [table.drop(engine) for table in tables]
        engine.dispose()
