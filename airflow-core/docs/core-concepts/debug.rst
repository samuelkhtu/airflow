 .. Licensed to the Apache Software Foundation (ASF) under one
    or more contributor license agreements.  See the NOTICE file
    distributed with this work for additional information
    regarding copyright ownership.  The ASF licenses this file
    to you under the Apache License, Version 2.0 (the
    "License"); you may not use this file except in compliance
    with the License.  You may obtain a copy of the License at

 ..   http://www.apache.org/licenses/LICENSE-2.0

 .. Unless required by applicable law or agreed to in writing,
    software distributed under the License is distributed on an
    "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
    KIND, either express or implied.  See the License for the
    specific language governing permissions and limitations
    under the License.

.. _concepts:debugging:

Debugging Airflow dags
======================

Testing dags with dag.test()
*****************************

To debug dags in an IDE, you can set up the ``dag.test`` command in your dag file and run through your DAG in a single
serialized python process.

This approach can be used with any supported database (including a local SQLite database) and will
*fail fast* as all tasks run in a single process.

To set up ``dag.test``, add these two lines to the bottom of your dag file:

.. code-block:: python

  if __name__ == "__main__":
      dag.test()

and that's it! You can add optional arguments to fine tune the testing but otherwise you can run or debug dags as
needed. Here are some examples of arguments:

* ``execution_date`` if you want to test argument-specific DAG runs
* ``use_executor`` if you want to test the DAG using an executor. By default ``dag.test`` runs the DAG without an
  executor, it just runs all the tasks locally.
  By providing this argument, the DAG is executed using the executor configured in the Airflow environment.

Conditionally skipping tasks
----------------------------

If you don't wish to execute some subset of tasks in your local environment (e.g. dependency check sensors or cleanup steps),
you can automatically mark them successful supplying a pattern matching their ``task_id`` in the ``mark_success_pattern`` argument.

In the following example, testing the dag won't wait for either of the upstream dags to complete. Instead, testing data
is manually ingested. The cleanup step is also skipped, making the intermediate csv is available for inspection.

.. code-block:: python

  with DAG("example_dag", default_args=default_args) as dag:
      sensor = ExternalTaskSensor(task_id="wait_for_ingestion_dag", external_dag_id="ingest_raw_data")
      sensor2 = ExternalTaskSensor(task_id="wait_for_dim_dag", external_dag_id="ingest_dim")
      collect_stats = PythonOperator(task_id="extract_stats_csv", python_callable=extract_stats_csv)
      # ... run other tasks
      cleanup = PythonOperator(task_id="cleanup", python_callable=Path.unlink, op_args=[collect_stats.output])

      [sensor, sensor2] >> collect_stats >> cleanup

  if __name__ == "__main__":
      ingest_testing_data()
      run = dag.test(mark_success_pattern="wait_for_.*|cleanup")
      print(f"Intermediate csv: {run.get_task_instance('collect_stats').xcom_pull(task_id='collect_stats')}")


Debugging Airflow dags on the command line
******************************************

With the same two line addition as mentioned in the above section, you can now easily debug a DAG using pdb as well.
Run ``python -m pdb <path to dag file>.py`` for an interactive debugging experience on the command line.

.. code-block:: bash

  root@ef2c84ad4856:/opt/airflow# python -m pdb providers/standard/src/airflow/providers/standard/example_dags/example_bash_operator.py
  > /opt/airflow/providers/standard/src/airflow/providers/standard/example_dags/example_bash_operator.py(18)<module>()
  -> """Example DAG demonstrating the usage of the BashOperator."""
  (Pdb) b 45
  Breakpoint 1 at /opt/airflow/providers/standard/src/airflow/providers/standard/example_dags/example_bash_operator.py:45
  (Pdb) c
  > /opt/airflow/providers/standard/src/airflow/providers/standard/example_dags/example_bash_operator.py(45)<module>()
  -> task_id="run_after_loop",
  (Pdb) run_this_last
  <Task(EmptyOperator): run_this_last>

**IDE setup steps:**

1. Add ``main`` block at the end of your DAG file to make it runnable.

.. code-block:: python

  if __name__ == "__main__":
      dag.test()

2. Run / debug the DAG file.
