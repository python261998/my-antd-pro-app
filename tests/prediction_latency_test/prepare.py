import os
import atexit
import importlib.util
import time
import csv
from subprocess import Popen

import pandas as pd
import docker
import requests
import psutil

import schemas as schema
from config import CONFIG


class Dataset:
    def __init__(self, name, **kwargs):
        self.name = name
        self.target = kwargs.get("target")
        self.handler_file = kwargs.get("handler_file", None)
        if self.handler_file is not None:
            self.handler = self._get_handler()
        else:
            self.handler = None

    def _get_handler(self):
        spec = importlib.util.spec_from_file_location("common", os.path.abspath(self.handler_file))
        handler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(handler)
        return handler.handler


DATASETS_PATH = os.getenv("DATASETS_PATH")
CONFIG_PATH = os.getenv("CONFIG_PATH")
datasets = [Dataset(key, **CONFIG['datasets'][key]) for key  in CONFIG['datasets'].keys()]


class Datasource:
    def __init__(self, name):
        self.name = name
        self.base_url = "http://127.0.0.1:47334/api/datasources/"
        self.url = self.base_url + name

    def upload(self, force=False):
        if force or not self.exists:
            self.delete()
            files = {}
            file_name = f"{self.name}.csv"
            with open(file_name, 'r') as fd:
                files['file'] = (file_name, fd, 'text/csv')
                files['source_type'] = (None, 'file')
                files['source'] = (None, file_name)
                print(f"calling {self.url} with files={files}")
                res = requests.put(self.url, files=files)
                res.raise_for_status()

    @property
    def list_datasources(self):
        res = requests.get(self.base_url)
        res.raise_for_status()
        return [x["name"] for x in res.json()]

    @property
    def exists(self):
        return self.name in self.list_datasources

    def delete(self):
        requests.delete(self.url)


class Predictor():
    def __init__(self, name):
        self.name = name
        self.base_url = "http://127.0.0.1:47334/api"
        self.url = f'{self.base_url}/predictors/{self.name}'

    def get_info(self):
        return requests.get(self.url).json()

    def is_ready(self):
        return self.get_info()["status"] == 'complete'

    @property
    def exists(self):
        return "status" in self.get_info()

    def learn(self, to_predict, force=False):
        if force or not self.exists:
            self.delete()
            datasource_name = f"{self.name}_train"
            res = requests.put(self.url, json={
                'data_source_name': datasource_name,
                'to_predict': to_predict,
                'stop_training_in_x_seconds': 10,
            })
            res.raise_for_status()

    def delete(self):
        requests.delete(self.url)


def monthly_sunspots_handler(df):
    months = df['Month']
    for i, val in enumerate(months):
        months[i] = val + "-01"


def get_handler(handler_path):
    spec = importlib.util.spec_from_file_location("common", os.path.abspath(handler_path))
    handler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handler)
    return handler.handler


def add_integration():
    db_info = CONFIG['database']
    db_info['enabled'] = True
    db_info['type'] = 'clickhouse'
    url = "http://127.0.0.1:47334/api/config/integrations/prediction_clickhouse"
    exist_request = requests.get(url)
    if exist_request.status_code == requests.status_codes.codes.ok:
        print("integration is already exists")
        return
    res = requests.put(url, json={'params': db_info})
    res.raise_for_status()


def split_datasets():
    for dataset in datasets:
        data_path = os.path.join(DATASETS_PATH, dataset.name, "data.csv")
        df = pd.read_csv(data_path)
        if dataset.handler is not None:
            dataset.handler(df)
        all_len = len(df)
        train_len = int(float(all_len) * 0.8)
        train_df = df[:train_len]
        test_df = df[train_len:]
        test_df = test_df.drop(columns=[dataset.target,])
        train_df.to_csv(f"{dataset.name}_train.csv", index=False)
        test_df.to_csv(f"{dataset.name}_test.csv", index=False)


def upload_datasets(force=False):
    """Upload train dataset to mindsdb via API."""
    for dataset in datasets:
        datasource = Datasource(f"{dataset.name}_train")
        print(datasource.name)
        datasource.upload(force=force)


def create_predictors(force=False):
    predictors = []
    for dataset in datasets:
        predictor = Predictor(dataset.name)
        predictor.learn(dataset.target, force=force)
        predictors.append(predictor)

    while predictors:
        for predictor in predictors[:]:
            if predictor.is_ready():
                print(f"predictor {predictor.name} is ready")
                predictors.remove(predictor)
                continue
            time.sleep(5)

def stop_mindsdb(ppid):
    pprocess = psutil.Process(ppid)
    pids = [x.pid for x in pprocess.children(recursive=True)]
    pids.append(ppid)
    for pid in pids:
        try:
            os.kill(pid, 9)
        # process may be killed by OS due to some reasons in that moment
        except ProcessLookupError:
            pass

def run_mindsdb():
    sp = Popen(['python3', '-m', 'mindsdb', '--config', CONFIG_PATH],
               close_fds=True)

    time.sleep(30)
    atexit.register(stop_mindsdb, sp.pid)

def run_clickhouse():
    docker_client = docker.from_env(version='auto')
    image = "yandex/clickhouse-server:latest"
    container_params = {'name': 'clickhouse-latency-test',
            'remove': True,
            'network_mode': 'host',
            }
    container = docker_client.containers.run(image, detach=True, **container_params)
    atexit.register(container.stop)
    return container

def prepare_db():
    db = schema.database
    query(f'DROP DATABASE IF EXISTS {db}')
    query(f'CREATE DATABASE {db}')

    for dataset in datasets:
        query(schema.tables[dataset.name])
        with open(f'{dataset.name}_train.csv') as fp:
            csv_fp = csv.reader(fp)
            for i, row in enumerate(csv_fp):
                if i == 0:
                    continue

                for i in range(len(row)):
                    try:
                        if '.' in row[i]:
                            row[i] = float(row[i])
                        else:
                            if row[i].isdigit():
                                row[i] = int(row[i])
                    except Exception as e:
                        print(e)

                query('INSERT INTO ' + schema.database + '.' + dataset.name + ' VALUES ({})'.format(
                    str(row).lstrip('[').rstrip(']')
                ))

def query(query):

    if 'CREATE ' not in query.upper() and 'INSERT ' not in query.upper():
        query += ' FORMAT JSON'

    db_info = CONFIG['database']

    host = db_info['host']
    port = db_info['port']
    user = db_info['user']
    password = db_info['password']

    connect_string = f'http://{host}:{port}'

    params = {'user': user, 'password': password}

    res = requests.post(
        connect_string,
        data=query,
        params=params,
        headers={"Connection": "close"}
    )

    if res.status_code != 200:
        print(f"error uploading: {query}")
        print(res.text, res.status_code)
    assert res.status_code == 200
    return res.text

def prepare_env(prepare_data=True,
                use_docker=True,
                setup_db=True,
                train_models=True):
    if prepare_data:
        print("preparing_datasets")
        split_datasets()
    if use_docker:
        print("running docker")
        run_clickhouse()
        time.sleep(10)
    if setup_db:
        print("preparing db")
        prepare_db()
    print("running mindsdb")
    run_mindsdb()
    print("uploading train datasets to mindsdb")
    upload_datasets(force=prepare_data)
    create_predictors(force=train_models)
    add_integration()
