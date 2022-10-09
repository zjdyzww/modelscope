# Copyright (c) Alibaba, Inc. and its affiliates.

import os
import os.path as osp
from abc import ABC, abstractmethod
from functools import partial
from multiprocessing import Pool
from threading import Lock
from typing import Any, Dict, Generator, List, Mapping, Union

import numpy as np

from modelscope.models.base import Model
from modelscope.msdatasets import MsDataset
from modelscope.outputs import TASK_OUTPUTS
from modelscope.pipeline_inputs import TASK_INPUTS, check_input_type
from modelscope.preprocessors import Preprocessor
from modelscope.utils.config import Config
from modelscope.utils.constant import Frameworks, ModelFile
from modelscope.utils.device import (create_device, device_placement,
                                     verify_device)
from modelscope.utils.hub import read_config, snapshot_download
from modelscope.utils.import_utils import is_tf_available, is_torch_available
from modelscope.utils.logger import get_logger
from modelscope.utils.torch_utils import _find_free_port, _is_free_port
from .util import is_model, is_official_hub_path

if is_torch_available():
    import torch

if is_tf_available():
    pass

Tensor = Union['torch.Tensor', 'tf.Tensor']
Input = Union[str, tuple, MsDataset, 'Image.Image', 'numpy.ndarray']
InputModel = Union[str, Model]

logger = get_logger()


class Pipeline(ABC):

    def initiate_single_model(self, model):
        if isinstance(model, str):
            logger.info(f'initiate model from {model}')
        if isinstance(model, str) and is_official_hub_path(model):
            logger.info(f'initiate model from location {model}.')
            # expecting model has been prefetched to local cache beforehand
            return Model.from_pretrained(
                model, model_prefetched=True,
                device=self.device_name) if is_model(model) else model
        elif isinstance(model, Model):
            return model
        else:
            if model and not isinstance(model, str):
                raise ValueError(
                    f'model type for single model is either str or Model, but got type {type(model)}'
                )
            return model

    def initiate_multiple_models(self, input_models: List[InputModel]):
        models = []
        for model in input_models:
            models.append(self.initiate_single_model(model))
        return models

    def __init__(self,
                 config_file: str = None,
                 model: Union[InputModel, List[InputModel]] = None,
                 preprocessor: Union[Preprocessor, List[Preprocessor]] = None,
                 device: str = 'gpu',
                 auto_collate=True,
                 **kwargs):
        """ Base class for pipeline.

        If config_file is provided, model and preprocessor will be
        instantiated from corresponding config. Otherwise, model
        and preprocessor will be constructed separately.

        Args:
            config_file(str, optional): Filepath to configuration file.
            model: (list of) Model name or model object
            preprocessor: (list of) Preprocessor object
            device (str): device str, should be either cpu, cuda, gpu, gpu:X or cuda:X
            auto_collate (bool): automatically to convert data to tensor or not.
        """
        if config_file is not None:
            self.cfg = Config.from_file(config_file)

        verify_device(device)
        self.device_name = device

        if not isinstance(model, List):
            self.model = self.initiate_single_model(model)
            self.models = [self.model]
        else:
            self.model = None
            self.models = self.initiate_multiple_models(model)

        self.has_multiple_models = len(self.models) > 1
        self.preprocessor = preprocessor

        if self.model or (self.has_multiple_models and self.models[0]):
            self.framework = self._get_framework()
        else:
            self.framework = None

        if self.framework == Frameworks.torch:
            self.device = create_device(self.device_name)
        self._model_prepare = False
        self._model_prepare_lock = Lock()
        self._auto_collate = auto_collate

    def prepare_model(self):
        """ Place model on certain device for pytorch models before first inference
        """
        self._model_prepare_lock.acquire(timeout=600)

        def _prepare_single(model):
            if isinstance(model, torch.nn.Module):
                model.to(self.device)
                model.eval()
            elif hasattr(model, 'model') and isinstance(
                    model.model, torch.nn.Module):
                model.model.to(self.device)
                model.model.eval()

        if not self._model_prepare:
            # prepare model for pytorch
            if self.framework == Frameworks.torch:
                if self.has_multiple_models:
                    for m in self.models:
                        _prepare_single(m)
                else:
                    _prepare_single(self.model)
            self._model_prepare = True
        self._model_prepare_lock.release()

    def _get_framework(self) -> str:
        frameworks = []
        for m in self.models:
            if isinstance(m, Model):
                model_dir = m.model_dir
            else:
                assert isinstance(m,
                                  str), 'model should be either str or Model.'
                model_dir = m
            cfg_file = osp.join(model_dir, ModelFile.CONFIGURATION)
            cfg = Config.from_file(cfg_file)
            frameworks.append(cfg.framework)
        if not all(x == frameworks[0] for x in frameworks):
            raise ValueError(
                f'got multiple models, but they are in different frameworks {frameworks}'
            )

        return frameworks[0]

    def __call__(self, input: Union[Input, List[Input]], *args,
                 **kwargs) -> Union[Dict[str, Any], Generator]:
        # model provider should leave it as it is
        # modelscope library developer will handle this function

        # place model to cpu or gpu
        if (self.model or (self.has_multiple_models and self.models[0])):
            if not self._model_prepare:
                self.prepare_model()

        # simple showcase, need to support iterator type for both tensorflow and pytorch
        # input_dict = self._handle_input(input)

        # sanitize the parameters
        preprocess_params, forward_params, postprocess_params = self._sanitize_parameters(
            **kwargs)
        kwargs['preprocess_params'] = preprocess_params
        kwargs['forward_params'] = forward_params
        kwargs['postprocess_params'] = postprocess_params

        if isinstance(input, list):
            output = []
            for ele in input:
                output.append(self._process_single(ele, *args, **kwargs))

        elif isinstance(input, MsDataset):
            return self._process_iterator(input, *args, **kwargs)

        else:
            output = self._process_single(input, *args, **kwargs)
        return output

    def _sanitize_parameters(self, **pipeline_parameters):
        """
        this method should sanitize the keyword args to preprocessor params,
        forward params and postprocess params on '__call__' or '_process_single' method
        considered to be a normal classmethod with default implementation / output

        Default Returns:
            Dict[str, str]:  preprocess_params = {}
            Dict[str, str]:  forward_params = {}
            Dict[str, str]:  postprocess_params = pipeline_parameters
        """
        return {}, {}, pipeline_parameters

    def _process_iterator(self, input: Input, *args, **kwargs):
        for ele in input:
            yield self._process_single(ele, *args, **kwargs)

    def _collate_fn(self, data):
        return collate_fn(data, self.device)

    def _process_single(self, input: Input, *args, **kwargs) -> Dict[str, Any]:
        preprocess_params = kwargs.get('preprocess_params', {})
        forward_params = kwargs.get('forward_params', {})
        postprocess_params = kwargs.get('postprocess_params', {})
        self._check_input(input)
        out = self.preprocess(input, **preprocess_params)
        with device_placement(self.framework, self.device_name):
            if self.framework == Frameworks.torch:
                with torch.no_grad():
                    if self._auto_collate:
                        out = self._collate_fn(out)
                    out = self.forward(out, **forward_params)
            else:
                out = self.forward(out, **forward_params)

        out = self.postprocess(out, **postprocess_params)
        self._check_output(out)
        return out

    def _check_input(self, input):
        task_name = self.group_key
        if task_name in TASK_INPUTS:
            input_type = TASK_INPUTS[task_name]

            # if multiple input formats are defined, we first
            # found the one that match input data and check
            if isinstance(input_type, list):
                matched_type = None
                for t in input_type:
                    if type(t) == type(input):
                        matched_type = t
                        break
                if matched_type is None:
                    err_msg = 'input data format for current pipeline should be one of following: \n'
                    for t in input_type:
                        err_msg += f'{t}\n'
                    raise ValueError(err_msg)
                else:
                    input_type = matched_type

            if isinstance(input_type, str):
                check_input_type(input_type, input)
            elif isinstance(input_type, tuple):
                for t, input_ele in zip(input_type, input):
                    check_input_type(t, input_ele)
            elif isinstance(input_type, dict):
                for k in input_type.keys():
                    # allow single input for multi-modal models
                    if k in input:
                        check_input_type(input_type[k], input[k])
            else:
                raise ValueError(f'invalid input_type definition {input_type}')
        else:
            logger.warning(f'task {task_name} input definition is missing')

    def _check_output(self, input):
        # this attribute is dynamically attached by registry
        # when cls is registered in registry using task name
        task_name = self.group_key
        if task_name not in TASK_OUTPUTS:
            logger.warning(f'task {task_name} output keys are missing')
            return
        output_keys = TASK_OUTPUTS[task_name]
        missing_keys = []
        for k in output_keys:
            if k not in input:
                missing_keys.append(k)
        if len(missing_keys) > 0:
            raise ValueError(f'expected output keys are {output_keys}, '
                             f'those {missing_keys} are missing')

    def preprocess(self, inputs: Input, **preprocess_params) -> Dict[str, Any]:
        """ Provide default implementation based on preprocess_cfg and user can reimplement it
        """
        assert self.preprocessor is not None, 'preprocess method should be implemented'
        assert not isinstance(self.preprocessor, List),\
            'default implementation does not support using multiple preprocessors.'
        return self.preprocessor(inputs, **preprocess_params)

    def forward(self, inputs: Dict[str, Any],
                **forward_params) -> Dict[str, Any]:
        """ Provide default implementation using self.model and user can reimplement it
        """
        assert self.model is not None, 'forward method should be implemented'
        assert not self.has_multiple_models, 'default implementation does not support multiple models in a pipeline.'
        return self.model(inputs, **forward_params)

    @abstractmethod
    def postprocess(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """ If current pipeline support model reuse, common postprocess
            code should be write here.

        Args:
            inputs:  input data

        Return:
            dict of results:  a dict containing outputs of model, each
                output should have the standard output name.
        """
        raise NotImplementedError('postprocess')


class DistributedPipeline(Pipeline):
    """This pipeline is used to load multi gpu models.

    What will this class do:
    1. Read the global config from the configuration.json
    2. Set the multiprocessing method to spawn
    3. Open a multiprocessing pool of the world_size to instantiate model pieces.
    4. Set the master port and ip
    5. Call _instantiate_one to instantiate one model piece
        This method should be implemented by the derived class.
    6. After the forward method is called, do preprocess in main process
        and call _forward_one to collect results, and do
        post process in main process.

    NOTE: _instantiate_one and _forward_one are class methods, any derived class should implement them and
    store the model handler in the class field.
    """

    def __init__(self,
                 model: str = None,
                 preprocessor: Union[Preprocessor, List[Preprocessor]] = None,
                 auto_collate=True,
                 **kwargs):
        self.preprocessor = preprocessor
        self._model_prepare = False
        self._model_prepare_lock = Lock()
        self._auto_collate = auto_collate

        if os.path.exists(model):
            self.model_dir = model
        else:
            self.model_dir = snapshot_download(model)
        self.cfg = read_config(self.model_dir)
        self.world_size = self.cfg.model.world_size
        self.model_pool = None
        self.device_name = 'cpu'
        self.device = create_device(self.device_name)
        self.has_multiple_models = False
        self.framework = self.cfg.framework
        if torch.multiprocessing.get_start_method(allow_none=True) is None:
            torch.multiprocessing.set_start_method('spawn')

        ranks = list(range(self.world_size))
        self.model_pool = Pool(self.world_size)
        master_ip = '127.0.0.1' if 'master_ip' not in kwargs else kwargs[
            'master_ip']
        master_port = '29500' if 'master_port' not in kwargs else kwargs[
            'master_port']
        if not _is_free_port(int(master_port)):
            master_port = str(_find_free_port())
        self.model_pool.map(
            partial(
                self.__class__._instantiate_one,
                model_dir=self.model_dir,
                master_ip=master_ip,
                master_port=master_port,
                **self.cfg.model,
                **kwargs), ranks)

    def __del__(self):
        if hasattr(self, 'model_pool') and self.model_pool is not None:
            self.model_pool.terminate()

    def __getstate__(self):
        self_dict = self.__dict__.copy()
        del self_dict['model_pool']
        del self_dict['preprocessor']
        del self_dict['_model_prepare_lock']
        return self_dict

    @classmethod
    def _instantiate_one(cls, rank, model_dir, **kwargs):
        """Instantiate one model piece.

        @param rank: The model rank.
        @param model_dir: The model_dir in the node.
        @param kwargs: Any extra args.
        @return: None. The model handler should be kept in the class field.
        """
        pass

    def forward(self, inputs: Dict[str, Any],
                **forward_params) -> Dict[str, Any]:
        inputs = {
            'inputs': inputs,
            'forward_params': forward_params,
        }
        res = self.model_pool.map(self.__class__._forward_one,
                                  [inputs] * self.world_size)
        return res[0]

    @classmethod
    def _forward_one(cls, inputs):
        """Forward the inputs to one model piece.

        Use the model handler kept in the class field to forward.

        @param inputs: The inputs after the preprocessing.
        @return: The forward results.
        """
        pass


def collate_fn(data, device):
    """Prepare the input just before the forward function.
    This method will move the tensors to the right device.
    Usually this method does not need to be overridden.

    Args:
        data: The data out of the dataloader.
        device: The device to move data to.

    Returns: The processed data.

    """
    from torch.utils.data.dataloader import default_collate
    from modelscope.preprocessors import InputFeatures
    if isinstance(data, dict) or isinstance(data, Mapping):
        return type(data)({k: collate_fn(v, device) for k, v in data.items()})
    elif isinstance(data, (tuple, list)):
        if isinstance(data[0], (int, float)):
            return default_collate(data).to(device)
        else:
            return type(data)(collate_fn(v, device) for v in data)
    elif isinstance(data, np.ndarray):
        if data.dtype.type is np.str_:
            return data
        else:
            return collate_fn(torch.from_numpy(data), device)
    elif isinstance(data, torch.Tensor):
        return data.to(device)
    elif isinstance(data, (bytes, str, int, float, bool, type(None))):
        return data
    elif isinstance(data, InputFeatures):
        return data
    else:
        import mmcv
        if isinstance(data, mmcv.parallel.data_container.DataContainer):
            return data
        else:
            raise ValueError(f'Unsupported data type {type(data)}')
