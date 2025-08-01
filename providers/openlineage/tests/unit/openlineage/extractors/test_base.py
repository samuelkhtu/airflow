# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from attrs import Factory, define, field
from openlineage.client.event_v2 import Dataset
from openlineage.client.facet_v2 import BaseFacet, JobFacet, parent_run, sql_job

from tests_common.test_utils.version_compat import AIRFLOW_V_3_0_PLUS

if AIRFLOW_V_3_0_PLUS:
    from airflow.sdk import BaseOperator
else:
    from airflow.models.baseoperator import BaseOperator  # type: ignore[no-redef]

from airflow.models.taskinstance import TaskInstanceState
from airflow.providers.openlineage.extractors.base import (
    BaseExtractor,
    DefaultExtractor,
    OperatorLineage,
)
from airflow.providers.openlineage.extractors.manager import ExtractorManager
from airflow.providers.openlineage.extractors.python import PythonExtractor

from tests_common.test_utils.compat import PythonOperator

if TYPE_CHECKING:
    from openlineage.client.facet_v2 import RunFacet


INPUTS = [Dataset(namespace="database://host:port", name="inputtable")]
OUTPUTS = [Dataset(namespace="database://host:port", name="inputtable")]
RUN_FACETS: dict[str, RunFacet] = {
    "parent": parent_run.ParentRunFacet(
        run=parent_run.Run(runId="3bb703d1-09c1-4a42-8da5-35a0b3216072"),
        job=parent_run.Job(namespace="namespace", name="parentjob"),
    )
}
JOB_FACETS: dict[str, JobFacet] = {"sql": sql_job.SQLJobFacet(query="SELECT * FROM inputtable")}


@define
class CompleteRunFacet(JobFacet):
    finished: bool = field(default=False)


@define
class FailRunFacet(JobFacet):
    failed: bool = field(default=False)


FINISHED_FACETS: dict[str, JobFacet] = {"complete": CompleteRunFacet(True)}
FAILED_FACETS: dict[str, JobFacet] = {"failure": FailRunFacet(True)}


class ExtractorWithoutExecuteOnFailure(BaseExtractor):
    @classmethod
    def get_operator_classnames(cls):
        return ["SimpleCustomOperator"]

    def _execute_extraction(self) -> OperatorLineage | None:
        return OperatorLineage(
            inputs=INPUTS,
            outputs=OUTPUTS,
            run_facets=RUN_FACETS,
            job_facets=JOB_FACETS,
        )

    def extract_on_complete(self, task_instance) -> OperatorLineage | None:
        return OperatorLineage(
            inputs=INPUTS,
            outputs=OUTPUTS,
            run_facets=RUN_FACETS,
            job_facets=FINISHED_FACETS,
        )


class ExtractorWithExecuteExtractionOnly(BaseExtractor):
    @classmethod
    def get_operator_classnames(cls):
        return ["AnotherOperator"]

    def _execute_extraction(self) -> OperatorLineage | None:
        return OperatorLineage(
            inputs=INPUTS,
            outputs=OUTPUTS,
            run_facets=RUN_FACETS,
            job_facets=JOB_FACETS,
        )


class SimpleCustomOperator(BaseOperator):
    def execute(self, context) -> Any:
        pass

    def get_openlineage_facets_on_start(self) -> OperatorLineage:
        return OperatorLineage()

    def get_openlineage_facets_on_complete(self, task_instance) -> OperatorLineage:
        return OperatorLineage()

    def get_openlineage_facets_on_failure(self, task_instance) -> OperatorLineage:
        return OperatorLineage()


class OperatorWithoutFailure(BaseOperator):
    def execute(self, context) -> Any:
        pass

    def get_openlineage_facets_on_start(self) -> OperatorLineage:
        return OperatorLineage(
            inputs=INPUTS,
            outputs=OUTPUTS,
            run_facets=RUN_FACETS,
            job_facets=JOB_FACETS,
        )

    def get_openlineage_facets_on_complete(self, task_instance) -> OperatorLineage:
        return OperatorLineage(
            inputs=INPUTS,
            outputs=OUTPUTS,
            run_facets=RUN_FACETS,
            job_facets=FINISHED_FACETS,
        )


class OperatorWithAllOlMethods(BaseOperator):
    def execute(self, context) -> Any:
        pass

    def get_openlineage_facets_on_start(self) -> OperatorLineage:
        return OperatorLineage(
            inputs=INPUTS,
            outputs=OUTPUTS,
            run_facets=RUN_FACETS,
            job_facets=JOB_FACETS,
        )

    def get_openlineage_facets_on_complete(self, task_instance) -> OperatorLineage:
        return OperatorLineage(
            inputs=INPUTS,
            outputs=OUTPUTS,
            run_facets=RUN_FACETS,
            job_facets=FINISHED_FACETS,
        )

    def get_openlineage_facets_on_failure(self, task_instance) -> OperatorLineage:
        return OperatorLineage(
            inputs=INPUTS,
            outputs=OUTPUTS,
            run_facets=RUN_FACETS,
            job_facets=FAILED_FACETS,
        )


class OperatorWithoutComplete(BaseOperator):
    def execute(self, context) -> Any:
        pass

    def get_openlineage_facets_on_start(self) -> OperatorLineage:
        return OperatorLineage(
            inputs=INPUTS,
            outputs=OUTPUTS,
            run_facets=RUN_FACETS,
            job_facets=JOB_FACETS,
        )


class OperatorWithoutStart(BaseOperator):
    def execute(self, context) -> Any:
        pass

    def get_openlineage_facets_on_complete(self, task_instance) -> OperatorLineage:
        return OperatorLineage(
            inputs=INPUTS,
            outputs=OUTPUTS,
            run_facets=RUN_FACETS,
            job_facets=FINISHED_FACETS,
        )


class OperatorDifferentOperatorLineageClass(BaseOperator):
    def execute(self, context) -> Any:
        pass

    def get_openlineage_facets_on_start(self):
        @define
        class DifferentOperatorLineage:
            name: str = ""
            inputs: list[Dataset] = Factory(list)
            outputs: list[Dataset] = Factory(list)
            run_facets: dict[str, BaseFacet] = Factory(dict)
            job_facets: dict[str, BaseFacet] = Factory(dict)
            some_other_param: dict = Factory(dict)

        return DifferentOperatorLineage(
            name="unused",
            inputs=INPUTS,
            outputs=OUTPUTS,
            run_facets=RUN_FACETS,
            job_facets=JOB_FACETS,
            some_other_param={"asdf": "fdsa"},
        )


class OperatorWrongOperatorLineageClass(BaseOperator):
    def execute(self, context) -> Any:
        pass

    def get_openlineage_facets_on_start(self):
        @define
        class WrongOperatorLineage:
            inputs: list[Dataset] = Factory(list)
            outputs: list[Dataset] = Factory(list)
            some_other_param: dict = Factory(dict)

        return WrongOperatorLineage(
            inputs=INPUTS,
            outputs=OUTPUTS,
            some_other_param={"asdf": "fdsa"},
        )


class BrokenOperator(BaseOperator):
    get_openlineage_facets: list[BaseFacet] = []

    def execute(self, context) -> Any:
        pass


def test_default_extraction():
    extractor = ExtractorManager().get_extractor_class(OperatorWithoutFailure)
    assert extractor is DefaultExtractor

    metadata = extractor(OperatorWithoutFailure(task_id="test")).extract()

    task_instance = mock.MagicMock()

    metadata_on_complete = extractor(OperatorWithoutFailure(task_id="test")).extract_on_complete(
        task_instance=task_instance
    )

    assert metadata == OperatorLineage(
        inputs=INPUTS,
        outputs=OUTPUTS,
        run_facets=RUN_FACETS,
        job_facets=JOB_FACETS,
    )

    assert metadata_on_complete == OperatorLineage(
        inputs=INPUTS,
        outputs=OUTPUTS,
        run_facets=RUN_FACETS,
        job_facets=FINISHED_FACETS,
    )


def test_extraction_without_on_complete():
    extractor = ExtractorManager().get_extractor_class(OperatorWithoutComplete)
    assert extractor is DefaultExtractor

    metadata = extractor(OperatorWithoutComplete(task_id="test")).extract()

    task_instance = mock.MagicMock()

    metadata_on_complete = extractor(OperatorWithoutComplete(task_id="test")).extract_on_complete(
        task_instance=task_instance
    )

    expected_task_metadata = OperatorLineage(
        inputs=INPUTS,
        outputs=OUTPUTS,
        run_facets=RUN_FACETS,
        job_facets=JOB_FACETS,
    )

    assert metadata == expected_task_metadata

    assert metadata_on_complete == expected_task_metadata


def test_extraction_without_on_start():
    extractor = ExtractorManager().get_extractor_class(OperatorWithoutStart)
    assert extractor is DefaultExtractor

    metadata = extractor(OperatorWithoutStart(task_id="test")).extract()

    task_instance = mock.MagicMock()

    metadata_on_complete = extractor(OperatorWithoutStart(task_id="test")).extract_on_complete(
        task_instance=task_instance
    )

    assert metadata == OperatorLineage()

    assert metadata_on_complete == OperatorLineage(
        inputs=INPUTS,
        outputs=OUTPUTS,
        run_facets=RUN_FACETS,
        job_facets=FINISHED_FACETS,
    )


@pytest.mark.parametrize(
    "operator_class, task_state, expected_job_facets",
    (
        (OperatorWithAllOlMethods, TaskInstanceState.FAILED, FAILED_FACETS),
        (OperatorWithAllOlMethods, TaskInstanceState.RUNNING, JOB_FACETS),
        (OperatorWithAllOlMethods, TaskInstanceState.SUCCESS, FINISHED_FACETS),
        (OperatorWithAllOlMethods, TaskInstanceState.UP_FOR_RETRY, FINISHED_FACETS),  # Should never happen
        (OperatorWithAllOlMethods, None, FINISHED_FACETS),  # Should never happen
        (OperatorWithoutFailure, TaskInstanceState.FAILED, FINISHED_FACETS),
        (OperatorWithoutFailure, TaskInstanceState.RUNNING, JOB_FACETS),
        (OperatorWithoutFailure, TaskInstanceState.SUCCESS, FINISHED_FACETS),
        (OperatorWithoutFailure, TaskInstanceState.UP_FOR_RETRY, FINISHED_FACETS),  # Should never happen
        (OperatorWithoutFailure, None, FINISHED_FACETS),  # Should never happen
        (OperatorWithoutStart, TaskInstanceState.FAILED, FINISHED_FACETS),
        (OperatorWithoutStart, TaskInstanceState.RUNNING, {}),
        (OperatorWithoutStart, TaskInstanceState.SUCCESS, FINISHED_FACETS),
        (OperatorWithoutStart, TaskInstanceState.UP_FOR_RETRY, FINISHED_FACETS),  # Should never happen
        (OperatorWithoutStart, None, FINISHED_FACETS),  # Should never happen
        (OperatorWithoutComplete, TaskInstanceState.FAILED, JOB_FACETS),
        (OperatorWithoutComplete, TaskInstanceState.RUNNING, JOB_FACETS),
        (OperatorWithoutComplete, TaskInstanceState.SUCCESS, JOB_FACETS),
        (OperatorWithoutComplete, TaskInstanceState.UP_FOR_RETRY, JOB_FACETS),  # Should never happen
        (OperatorWithoutComplete, None, JOB_FACETS),  # Should never happen
    ),
)
def test_extractor_manager_calls_appropriate_extractor_method(
    operator_class, task_state, expected_job_facets
):
    extractor_manager = ExtractorManager()

    ti = mock.MagicMock()

    metadata = extractor_manager.extract_metadata(
        dagrun=mock.MagicMock(run_id="dagrun_run_id"),
        task=operator_class(task_id="task_id"),
        task_instance_state=task_state,
        task_instance=ti,
    )

    assert metadata.job_facets == expected_job_facets
    if not expected_job_facets:  # Empty OperatorLineage() is expected
        assert not metadata.inputs
        assert not metadata.outputs
        assert not metadata.run_facets
    else:
        assert metadata.inputs == INPUTS
        assert metadata.outputs == OUTPUTS
        assert metadata.run_facets == RUN_FACETS


@mock.patch("airflow.providers.openlineage.conf.custom_extractors")
def test_extractors_env_var(custom_extractors):
    custom_extractors.return_value = {
        "unit.openlineage.extractors.test_base.ExtractorWithoutExecuteOnFailure"
    }
    extractor = ExtractorManager().get_extractor_class(SimpleCustomOperator(task_id="example"))
    assert extractor is ExtractorWithoutExecuteOnFailure


def test_extractor_without_extract_on_failure_calls_extract_on_complete():
    extractor = ExtractorWithoutExecuteOnFailure(SimpleCustomOperator(task_id="example"))
    result = extractor.extract_on_failure(None)
    assert result == OperatorLineage(
        inputs=INPUTS,
        outputs=OUTPUTS,
        run_facets=RUN_FACETS,
        job_facets=FINISHED_FACETS,
    )


def test_extractor_without_extract_on_complete_and_failure_always_calls_extract():
    extractor = ExtractorWithExecuteExtractionOnly(SimpleCustomOperator(task_id="example"))
    expected_result = OperatorLineage(
        inputs=INPUTS,
        outputs=OUTPUTS,
        run_facets=RUN_FACETS,
        job_facets=JOB_FACETS,
    )
    result = extractor.extract_on_failure(None)
    assert result == expected_result
    result = extractor.extract_on_complete(None)
    assert result == expected_result
    result = extractor.extract()
    assert result == expected_result


def test_does_not_use_default_extractor_when_not_a_method():
    extractor_class = ExtractorManager().get_extractor_class(BrokenOperator(task_id="a"))
    assert extractor_class is None


def test_does_not_use_default_extractor_when_no_get_openlineage_facets():
    extractor_class = ExtractorManager().get_extractor_class(BaseOperator(task_id="b"))
    assert extractor_class is None


def test_does_not_use_default_extractor_when_explicit_extractor():
    extractor_class = ExtractorManager().get_extractor_class(
        PythonOperator(task_id="c", python_callable=lambda: 7)
    )
    assert extractor_class is PythonExtractor


def test_default_extractor_uses_different_operatorlineage_class():
    operator = OperatorDifferentOperatorLineageClass(task_id="task_id")
    extractor_class = ExtractorManager().get_extractor_class(operator)
    assert extractor_class is DefaultExtractor
    extractor = extractor_class(operator)
    assert extractor.extract() == OperatorLineage(
        inputs=INPUTS,
        outputs=OUTPUTS,
        run_facets=RUN_FACETS,
        job_facets=JOB_FACETS,
    )


def test_default_extractor_uses_wrong_operatorlineage_class():
    operator = OperatorWrongOperatorLineageClass(task_id="task_id")
    # If extractor returns lineage class that can't be changed into OperatorLineage, just return
    # empty OperatorLineage
    assert ExtractorManager().extract_metadata(mock.MagicMock(), operator, None) == OperatorLineage()
