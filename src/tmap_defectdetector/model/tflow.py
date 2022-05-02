from __future__ import annotations


import os
import sys
import time
from abc import abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Optional, ClassVar, Collection, Callable, TypeAlias

import pandas as pd
import tensorflow as tf
import numpy as np
import matplotlib.pyplot as plt
from numpy import ndarray
from pandas import DataFrame
from tensorflow.python.framework.errors_impl import InternalError

from tmap_defectdetector import DEFAULT_ELPV_MODELPATH
from tmap_defectdetector.dataset.base.dataset_configs_base import ImageDataSetConfig
from tmap_defectdetector.dataset.datasets import ImageDataSetELPV
from tmap_defectdetector.dataset.base.datasets_base import DefectDetectionDataSetImages
from tmap_defectdetector.image.checks import ImageDimensionError
from tmap_defectdetector.image.color import rgb_to_grayscale, grayscale_to_binary
from tmap_defectdetector.logger import log
from tmap_defectdetector.model import GPU_AVAILABLE, DEFAULT_GPU
from tmap_defectdetector.model.plots import plot_image, plot_value_array

if TYPE_CHECKING:
    from build.lib.tmap_defectdetector.dataset.dataset_configs import DataSetConfigELPV


class DefectDetectionModel:
    @classmethod
    @abstractmethod
    def from_dataset(cls, dataset: ImageDataSetELPV) -> CNNModel:
        return NotImplemented


class SplitDataSet(NamedTuple):
    train: DataFrame
    test: DataFrame


TensorFlowMetric: TypeAlias = Collection[str | Callable[[float, float], any] | tf.keras.metrics.Metric]
TensorFlowLossFunction: TypeAlias = tf.keras.losses.Loss | str
TensorFlowPredictions: TypeAlias = list[ndarray]


class ValidImgType(Enum):
    BINARY: str = "binary"
    GRAYSCALE: str = "grayscale"
    RGB: str = "rgb"


@dataclass
class CNNModelConfig:

    n_epochs: int = 1024 if GPU_AVAILABLE else 64
    training_frac: float = 0.65
    n_nodes_layer2: int = 128
    n_nodes_layer3: int = 10
    activation_func_id: str = "relu"
    optimizer: str = "adam"
    metrics: TensorFlowMetric = ("accuracy",)
    loss_function: TensorFlowLossFunction = tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True)
    _img_type: ValidImgType = ValidImgType.BINARY

    @property
    def img_type(self) -> ValidImgType:
        return self._img_type

    @img_type.setter
    def img_type(self, img_type: ValidImgType):
        if not isinstance(img_type, ValidImgType):
            raise ValueError(
                f"Invalid image type for parameter 'img_type'. Valid: {', '.join(x.name for x in ValidImgType)}"
            )
        self._img_type = img_type


class CNNModel(DefectDetectionModel):

    FLOAT_COMP_TOL: ClassVar[float] = 1e-8
    """Float comparison error tolerance."""

    def __init__(
        self,
        dataset: DefectDetectionDataSetImages,
        training_frac: float = 0.65,
        model_config: CNNModelConfig = CNNModelConfig(),
    ):
        """
        Convolutional Neural Network model for image-based defect detection.

        :param dataset: DefectDetectionDataSetImages (derived) instance containing
            labeled samples for training.
        :param training_frac: (optional) fraction of data to use
            for training vs. test/validation data (Default = .65).
        :param model_config: (optional) container storing configuration for the CNN Model
            (Default = CNNModelConfig instance).
        """
        self.training_frac: float = training_frac

        self.dataset: DefectDetectionDataSetImages = dataset

        self.training_data: DataFrame = pd.DataFrame()
        self.test_data: DataFrame = pd.DataFrame()

        self.cfg: ImageDataSetConfig = self.dataset.dataset_cfg
        self.id_col: str = self.cfg.SCHEMA_LABELS.LABEL_SAMPLE_ID.name
        self.model_config: CNNModelConfig = model_config

        self._grayscale: bool = False

    @property
    def data(self) -> pd.DataFrame:
        """Returns the full dataset (samples + labels) as pandas DataFrame."""
        return self.dataset.data

    def init_training_and_test_sets(self):
        self.training_data, self.test_data = self._get_training_and_test_sets()
        if not (len(self.training_data) + len(self.test_data)) == len(self.data):
            raise RuntimeError("Splitting of data into test/training sets resulted in information loss.")

    def _get_training_and_test_sets(self, frac: Optional[float] = None) -> SplitDataSet:
        """
        Splits the dataset into training and test/validation parts according to specified fraction.

        :param frac: fraction of data to use for training (Default = None -> self.training_frac attribute)
        """
        if frac is None:
            frac = self.training_frac
        log.info(f"Splitting dataset into training ({frac*100:.2f}%) and test ({(1-frac)*100:.2f}%) parts.")
        return SplitDataSet(
            train=(img_train := self.data.sample(frac=frac)),
            test=pd.concat([self.data, img_train]).drop_duplicates(subset=[self.id_col], keep=False),
        )

    @classmethod
    def from_dataset(cls, dataset: ImageDataSetELPV) -> CNNModel:
        raise NotImplementedError(f"{cls.__name__}'s factory method not yet implemented.")

    def amplify_data(self, mirror_axes: tuple[int, int, int, int] = (1, 2, 3, 4)) -> None:
        """Amplifies data for current selected dataset"""
        self.dataset.amplify_data(mirror_axes=mirror_axes)

    def filter(self, query: str) -> None:
        self.dataset.filter(query=query)


class DataByLabelsELPV(NamedTuple):
    pass_: DataFrame
    review: DataFrame
    fail: DataFrame


class ImageShape(NamedTuple):
    rows: int
    cols: int


class ModelEvaluation(NamedTuple):
    loss: float
    accuracy: float
    probability_model: tf.keras.Sequential
    predictions: TensorFlowPredictions


class CNNModelELPV(CNNModel):
    def __init__(
        self,
        dataset: ImageDataSetELPV,
        model_config: CNNModelConfig = CNNModelConfig(),
        class_names: Collection[str] = ("Fail", "Review_A", "Review_B", "Pass"),
    ):
        """
        This must be generalized later; we need a generalized implementation for all (image)
        datasets, but given the time constraint we just build a class specific for the ELPV dataset.
        """
        super().__init__(dataset=dataset, model_config=model_config)

        self.cfg: DataSetConfigELPV = dataset.dataset_cfg
        self.quality_col = self.cfg.SCHEMA_FULL.PROBABILITY.name
        self.type_col = self.cfg.SCHEMA_FULL.TYPE.name
        self.img_col = self.cfg.SCHEMA_FULL.SAMPLE.name
        self.class_names: Collection[str] = class_names

        self._model: Optional[tf.keras.models.Model] = None
        self._compiled: bool = False
        self._fitted: bool = False
        self._binary_colors: bool = False
        self._grayscale: bool = False

    def images_to_grayscale_nonreversible(self):
        """Non-reversibly converts images in the current dataset to grayscale."""
        if self._binary_colors:
            raise ValueError("Cannot convert to grayscale: already converted to binary colors.")
        if not self._grayscale:
            log.info("Converting images to grayscale...")
            if self.dataset.data.loc[:, self.img_col].iloc[0].shape[-1] == 3:
                self.dataset.data.loc[:, self.img_col] = self.dataset.data.loc[:, self.img_col].apply(
                    rgb_to_grayscale
                )
                log.info("Succesfully converted image dataset to grayscale.")
                self._grayscale = True
            log.info("Conversion to grayscale ignored; image doesn't have expected (rgb) input shape.")

    def images_to_binary(self):
        """Non-reversibly converts images in the current dataset to binary images."""
        if not self._grayscale:
            self.images_to_grayscale_nonreversible()

        log.info("Converting images to binary colors...")
        self.dataset.data.loc[:, self.img_col] = self.dataset.data.loc[:, self.img_col].apply(
            grayscale_to_binary
        )
        log.info("Succesfully converted image dataset to binary colors.")
        self._binary_colors = True
        log.info("Conversion to grayscale ignored; image doesn't have expected (rgb) input shape.")

    def unsqueeze_images(self) -> None:
        log.info("'Un'-squeezing images: NxM -> NxMx1")
        self.dataset.data.loc[:, self.img_col] = self.dataset.data.loc[:, self.img_col].apply(
            self._unsqueeze_img
        )

    @staticmethod
    def delete_saved_model():
        if DEFAULT_ELPV_MODELPATH.exists() and DEFAULT_ELPV_MODELPATH.is_file():
            os.remove(DEFAULT_ELPV_MODELPATH)

    def squeeze_images(self) -> None:
        log.info("Squeezing images: NxMx1 -> NxM")
        self.dataset.data.loc[:, self.img_col] = self.dataset.data.loc[:, self.img_col].apply(np.squeeze)

    @staticmethod
    def _unsqueeze_img(img: ndarray) -> ndarray:
        return img.reshape(*img.shape[:2], 1)

    @classmethod
    def from_dataset(cls, dataset: ImageDataSetELPV, tolerance: Optional[float] = None) -> CNNModelELPV:
        tolerance = cls.FLOAT_COMP_TOL if tolerance is None else tolerance
        raise NotImplementedError()

    def get_train_data_by_label(self, tolerance: Optional[float] = None) -> DataByLabelsELPV:
        tolerance = self.FLOAT_COMP_TOL if tolerance is None else tolerance
        self._ensure_train_imgdata()
        return DataByLabelsELPV(
            pass_=self.data[np.isclose(self.data[self.quality_col], 1.0, atol=tolerance)]
            .loc[:, self.img_col]
            .to_numpy(),
            review=self.data[self.data[self.quality_col].gt(0.01)].loc[:, self.img_col].to_numpy(),
            fail=self.data[np.isclose(self.data[self.quality_col], 0.0, atol=tolerance)]
            .loc[:, self.img_col]
            .to_numpy(),
        )

    def get_test_data_by_label(self, tolerance: float = None) -> DataByLabelsELPV:
        tolerance = self.FLOAT_COMP_TOL if tolerance is None else tolerance
        self._ensure_train_imgdata()
        return DataByLabelsELPV(
            pass_=self.data[np.isclose(self.data[self.quality_col], 1.0, atol=tolerance)]
            .loc[:, self.quality_col]
            .to_numpy(),
            review=self.data[self.data[self.quality_col].gt(0.01)].loc[:, self.quality_col].to_numpy(),
            fail=self.data[np.isclose(self.data[self.quality_col], 0.0, atol=tolerance)]
            .loc[:, self.quality_col]
            .to_numpy(),
        )

    @property
    def train_images(self) -> ndarray:
        self._ensure_train_imgdata()
        return np.stack(self.training_data.loc[:, self.img_col].to_numpy())

    @property
    def train_labels(self) -> ndarray:
        self._ensure_train_imgdata()
        return self.training_data.loc[:, self.quality_col].to_numpy()

    @property
    def test_images(self) -> ndarray:
        self._ensure_train_imgdata()
        return np.stack(self.training_data.loc[:, self.img_col].to_numpy())

    @property
    def test_labels(self) -> ndarray:
        self._ensure_train_imgdata()
        return self.training_data.loc[:, self.quality_col].to_numpy()

    @property
    def image_shape(self) -> ImageShape:
        shapes = list(set(image.shape for image in self.data.loc[:, self.img_col]))
        if len(shapes) > 1:
            self.squeeze_images()
        shapes = list(set(image.shape for image in self.data.loc[:, self.img_col]))
        if len(shapes) > 1:
            raise ValueError(
                "Cannot train images which occur in different shapes. "
                f"(Found shapes: {', '.join(repr(shape) for shape in shapes)})"
            )
        return ImageShape(*list(shapes)[0])

    def amplify_data(self, mirror_axes: tuple[int, int, int, int] = (1, 2, 3, 4)) -> None:
        """Amplifies data for current selected dataset"""
        try:
            self.dataset.amplify_data(mirror_axes=mirror_axes)
        except ImageDimensionError:
            self.unsqueeze_images()
            self.dataset.amplify_data(mirror_axes=mirror_axes)
            self.squeeze_images()

    def load(self, model_savepath: Path = DEFAULT_ELPV_MODELPATH) -> None:
        if model_savepath.exists() and self._model is not None:
            self._model.load_weights(model_savepath)
        elif not model_savepath.exists():
            raise FileNotFoundError(
                f"Couldn't load {type(self).__name__} model from non-existing file {str(model_savepath)!r}."
            )

    @classmethod
    def full_run_from_dataset(
        cls,
        dataset: ImageDataSetELPV,
        tolerance: Optional[float] = None,
        amplify_data: bool = True,
        model_savepath: Path = DEFAULT_ELPV_MODELPATH,
        force_retrain: bool = False,
    ) -> None:
        if tolerance is None:
            tolerance = cls.FLOAT_COMP_TOL
        tolerance: float

        model = cls(dataset)

        if len(set(dataset.labels[model.type_col])) != 1:
            raise ValueError("Training dataset for multiple types; not yet supported.")

        if model.model_config.img_type == ValidImgType.GRAYSCALE:
            model.images_to_grayscale_nonreversible()
        elif model.model_config.img_type == ValidImgType.BINARY:
            model.images_to_binary()

        if amplify_data:
            model.amplify_data()

        if model_savepath.exists() and not force_retrain:
            model.load()
            evaluation = model.evaluate()
        elif GPU_AVAILABLE:
            try:
                with tf.device(DEFAULT_GPU):
                    model.build()
                    evaluation: ModelEvaluation = model.evaluate()
            except InternalError:
                log.info(
                    "It is likely that an attempt to run the model on the GPU failed. Trying CPU training instead."
                )
                time.sleep(1)
                with tf.device("cpu:0"):
                    model.build()
                    evaluation: ModelEvaluation = model.evaluate()
        else:
            with tf.device("cpu:0"):
                model.build()
                evaluation: ModelEvaluation = model.evaluate()

        model._model.save_weights()
        try:
            model.plot(predictions=evaluation.predictions)
        except Exception as err:
            raise RuntimeError(f"Model was trained, but plotting failed: {str(err)!r}") from err

    def plot(self, predictions: TensorFlowPredictions):

        log.info(str(predictions[0]))
        log.info(str(np.argmax(predictions[0])))
        log.info(str(self.test_labels[0]))
        i = 0
        plt.figure(figsize=(6, 3))
        plt.subplot(1, 2, 1)
        plot_image(i, predictions[i], self.test_labels, self.test_images, class_names=self.class_names)
        plt.subplot(1, 2, 2)
        plot_value_array(i, predictions[i], self.test_labels)
        plt.show()

    def build(self) -> None:

        self._model = tf.keras.Sequential(
            [
                tf.keras.layers.Flatten(input_shape=self.image_shape),
                tf.keras.layers.Dense(
                    self.model_config.n_nodes_layer2,
                    activation=self.model_config.activation_func_id,
                    dtype=np.float32,
                ),
                tf.keras.layers.Dense(self.model_config.n_nodes_layer3),
            ]
        )

    def _compile(self):
        if self._model is None:
            self.build()

        self._model.compile(
            optimizer=self.model_config.optimizer,
            loss=self.model_config.loss_function,
            metrics=list(self.model_config.metrics),
        )
        self._compiled = True

    def _fit(self):
        if not self._compiled:
            self._compile()
        self._model.fit(self.train_images, self.train_labels, epochs=self.model_config.n_epochs)
        self._fitted = True

    def evaluate(self) -> ModelEvaluation:
        self._compile()
        self._fit()
        test_loss, test_acc = self._model.evaluate(self.test_images, self.test_labels, verbose=2)
        log.info(f"\nTest accuracy: {test_acc:.1f}")
        probability_model = tf.keras.Sequential([self._model, tf.keras.layers.Softmax()])
        predictions = probability_model.predict(self.test_images)
        return ModelEvaluation(
            loss=test_loss, accuracy=test_acc, probability_model=probability_model, predictions=predictions
        )

    def _ensure_train_imgdata(self) -> None:
        if self.training_data.empty:
            self.init_training_and_test_sets()
