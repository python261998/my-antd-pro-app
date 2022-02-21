import os
import traceback
import tempfile
from pathlib import Path
from typing import Optional
from numpy import isin
import json

import pandas as pd
from pandas.core.frame import DataFrame
import torch.multiprocessing as mp
import lightwood
from lightwood.api.types import ProblemDefinition, JsonAI
from lightwood import __version__ as lightwood_version

from mindsdb import __version__ as mindsdb_version
import mindsdb.interfaces.storage.db as db
from mindsdb.interfaces.database.database import DatabaseWrapper
from mindsdb.interfaces.model.model_interface import ModelInterface
from mindsdb.interfaces.storage.db import session, Predictor, Datasource
from mindsdb.interfaces.datastore.datastore import DataStore
from mindsdb.interfaces.storage.fs import FsStore
from mindsdb.utilities.config import Config
from mindsdb.utilities.functions import mark_process
from mindsdb.utilities.log import log
from mindsdb.utilities.with_kwargs_wrapper import WithKWArgsWrapper


ctx = mp.get_context('spawn')


def create_learn_mark():
    if os.name == 'posix':
        p = Path(tempfile.gettempdir()).joinpath('mindsdb/learn_processes/')
        p.mkdir(parents=True, exist_ok=True)
        p.joinpath(f'{os.getpid()}').touch()


def delete_learn_mark():
    if os.name == 'posix':
        p = Path(tempfile.gettempdir()).joinpath('mindsdb/learn_processes/').joinpath(f'{os.getpid()}')
        if p.exists():
            p.unlink()


def rep_recur(org: dict, ovr: dict):
    for k in ovr:
        if k in org:
            if isinstance(org[k], dict) and isinstance(ovr[k], dict):
                rep_recur(org[k], ovr[k])
            else:
                org[k] = ovr[k]
        else:
            org[k] = ovr[k]


def brack_to_mod(ovr):
    if not isinstance(ovr, dict):
        if isinstance(ovr, list):
            for i in range(len(ovr)):
                ovr[i] = brack_to_mod(ovr[i])
        elif isinstance(ovr, str):
            if '(' in ovr and ')' in ovr:
                mod = ovr.split('(')[0]
                args = {}
                if '()' not in ovr:
                    for str_pair in ovr.split('(')[1].split(')')[0].split(','):
                        k = str_pair.split('=')[0].strip(' ')
                        v = str_pair.split('=')[1].strip(' ')
                        args[k] = v

                ovr = {
                    'module': mod,
                    'args': args
                }
            elif '{' in ovr and '}' in ovr:
                try:
                    ovr = json.loads(ovr)
                except Exception:
                    pass
        return ovr
    else:
        for k in ovr.keys():
            ovr[k] = brack_to_mod(ovr[k])

    return ovr

@mark_process(name='learn')
def run_generate(df: DataFrame, problem_definition: ProblemDefinition, predictor_id: int, json_ai_override: dict = None) -> int:
    json_ai = lightwood.json_ai_from_problem(df, problem_definition)
    if json_ai_override is None:
        json_ai_override = {}

    json_ai_override = brack_to_mod(json_ai_override)
    json_ai = json_ai.to_dict()
    rep_recur(json_ai, json_ai_override)

    json_ai = JsonAI.from_dict(json_ai)

    code = lightwood.code_from_json_ai(json_ai)

    predictor_record = Predictor.query.with_for_update().get(predictor_id)
    predictor_record.json_ai = json_ai.to_dict()
    predictor_record.code = code
    db.session.commit()


@mark_process(name='learn')
def run_fit(predictor_id: int, df: pd.DataFrame) -> None:
    try:
        predictor_record = Predictor.query.with_for_update().get(predictor_id)
        assert predictor_record is not None

        fs_store = FsStore()
        config = Config()

        predictor_record.data = {'training_log': 'training'}
        session.commit()
        predictor: lightwood.PredictorInterface = lightwood.predictor_from_code(predictor_record.code)
        predictor.learn(df)

        session.refresh(predictor_record)

        fs_name = f'predictor_{predictor_record.company_id}_{predictor_record.id}'
        pickle_path = os.path.join(config['paths']['predictors'], fs_name)
        predictor.save(pickle_path)

        fs_store.put(fs_name, fs_name, config['paths']['predictors'])

        predictor_record.data = predictor.model_analysis.to_dict()
        predictor_record.dtype_dict = predictor.dtype_dict
        session.commit()

        dbw = DatabaseWrapper(predictor_record.company_id)
        mi = WithKWArgsWrapper(ModelInterface(), company_id=predictor_record.company_id)
    except Exception as e:
        session.refresh(predictor_record)
        predictor_record.data = {'error': f'{traceback.format_exc()}\nMain error: {e}'}
        session.commit()
        raise e

    try:
        dbw.register_predictors([mi.get_model_data(predictor_record.name)])
    except Exception as e:
        log.warn(e)


@mark_process(name='learn')
def run_learn(df: DataFrame, problem_definition: ProblemDefinition, predictor_id: int,
              delete_ds_on_fail: Optional[bool] = False, json_ai_override: dict = None) -> None:
    if json_ai_override is None:
        json_ai_override = {}
    try:
        run_generate(df, problem_definition, predictor_id, json_ai_override)
        run_fit(predictor_id, df)
    except Exception as e:
        predictor_record = Predictor.query.with_for_update().get(predictor_id)
        if delete_ds_on_fail is True:
            linked_db_ds = Datasource.query.filter_by(id=predictor_record.datasource_id).first()
            if linked_db_ds is not None:
                predictors_with_ds = Predictor.query.filter(
                    (Predictor.id != predictor_id) & (Predictor.datasource_id == linked_db_ds.id)
                ).all()
                if len(predictors_with_ds) == 0:
                    session.delete(linked_db_ds)
                    predictor_record.datasource_id = None
        predictor_record.data = {"error": str(e)}
        session.commit()


def run_adjust(name, db_name, from_data, datasource_id, company_id):
    # @TODO: Actually implement this
    return 0


@mark_process(name='learn')
def run_update(name: str, company_id: int):
    original_name = name
    name = f'{company_id}@@@@@{name}'

    fs_store = FsStore()
    config = Config()
    data_store = WithKWArgsWrapper(DataStore(), company_id=company_id)

    try:
        predictor_record = Predictor.query.filter_by(company_id=company_id, name=original_name).first()
        assert predictor_record is not None

        predictor_record.update_status = 'updating'

        session.commit()
        ds = data_store.get_datasource_obj(None, raw=False, id=predictor_record.datasource_id)
        df = ds.df

        problem_definition = predictor_record.learn_args

        problem_definition['target'] = predictor_record.to_predict[0]

        if 'join_learn_process' in problem_definition:
            del problem_definition['join_learn_process']

        # Adapt kwargs to problem definition
        if 'timeseries_settings' in problem_definition:
            problem_definition['timeseries_settings'] = problem_definition['timeseries_settings']

        if 'stop_training_in_x_seconds' in problem_definition:
            problem_definition['time_aim'] = problem_definition['stop_training_in_x_seconds']

        json_ai = lightwood.json_ai_from_problem(df, problem_definition)
        predictor_record.json_ai = json_ai.to_dict()
        predictor_record.code = lightwood.code_from_json_ai(json_ai)
        predictor_record.data = {'training_log': 'training'}
        session.commit()
        predictor: lightwood.PredictorInterface = lightwood.predictor_from_code(predictor_record.code)
        predictor.learn(df)

        fs_name = f'predictor_{predictor_record.company_id}_{predictor_record.id}'
        pickle_path = os.path.join(config['paths']['predictors'], fs_name)
        predictor.save(pickle_path)
        fs_store.put(fs_name, fs_name, config['paths']['predictors'])
        predictor_record.data = predictor.model_analysis.to_dict()  # type: ignore
        session.commit()

        predictor_record.lightwood_version = lightwood_version
        predictor_record.mindsdb_version = mindsdb_version
        predictor_record.update_status = 'up_to_date'
        session.commit()

    except Exception as e:
        log.error(e)
        predictor_record.update_status = 'update_failed'  # type: ignore
        session.commit()
        return str(e)


class LearnProcess(ctx.Process):
    daemon = True

    def __init__(self, *args):
        super(LearnProcess, self).__init__(args=args)

    def run(self):
        run_learn(*self._args)


class GenerateProcess(ctx.Process):
    daemon = True

    def __init__(self, *args):
        super(GenerateProcess, self).__init__(args=args)

    def run(self):
        run_generate(*self._args)


class FitProcess(ctx.Process):
    daemon = True

    def __init__(self, *args):
        super(FitProcess, self).__init__(args=args)

    def run(self):
        run_fit(*self._args)


class AdjustProcess(ctx.Process):
    daemon = True

    def __init__(self, *args):
        super(AdjustProcess, self).__init__(args=args)

    def run(self):
        '''
        running at subprocess due to
        ValueError: signal only works in main thread

        this is work for celery worker here?
        '''
        run_adjust(*self._args)


class UpdateProcess(ctx.Process):
    daemon = True

    def __init__(self, *args):
        super(UpdateProcess, self).__init__(args=args)

    def run(self):
        run_update(*self._args)
