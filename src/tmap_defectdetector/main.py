"""The main file containing the program's entrypoint."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from tmap_defectdetector.compatibility_checks import version_check
from tmap_defectdetector.dataset.datasets import ImageDataSetELPV
from tmap_defectdetector.dataset.downloaders import DataSetDownloaderELPV

from tmap_defectdetector.dataset.dataset_configs import DataSetConfigELPV
from tmap_defectdetector.dataset.schemas import SchemaLabelsELPV

from tmap_defectdetector.logger import log
from tmap_defectdetector.pathconfig.path_helpers import open_directory_with_filebrowser
from tmap_defectdetector.pathconfig.paths import DIR_TMP, TEXTUAL_LOGPATH
from tmap_defectdetector.tui.home import DefectDetectorTUI, DefectDetectorTUIApp

AVAILABLE_DOWNLOADERS = "ELPV"


def cli():
    """CLI is not yet implemented."""
    ...


def gui():
    """GUI is not yet implemented."""
    ...


def tui():
    """TUI is not yet implemented."""

    app = DefectDetectorTUIApp(dataset_configs=[DataSetConfigELPV()])
    app.run(title="Defect Detector - TMAP April 2022", log=TEXTUAL_LOGPATH)


def get_dataset(dataset_name: str = "elpv", url: Optional[str] = None):
    """Retrieves a DataSet object containing data either from the web, or from the"""
    ...


def example_elpv(save_and_open_amplified_dataset: bool = True):
    """
    Performs an example run which (down)loads the ELPV defect image dataset,
    amplifies it with mirroring, rotations, and translations, and then optionally
    shows it .

    :param save_and_open_amplified_dataset: (optional) flag to indicate whether to save
        the example amplified dataset as images to a temporary directory.
        Can take quite some time and space(default = False)
    """
    # Initialize the dataset downloader and download the ELPV dataset from its git repository.
    downloader = DataSetDownloaderELPV()
    downloader.download()  # The dataset is downloaded to %LOCALAPPDATA%/.tmapdd/datasets/dataset-elpv/ (on Windows)

    # Initialize/load the ELPV dataset using the ELPV dataset configuration.
    elpv_dataset_config = DataSetConfigELPV()
    dataset = ImageDataSetELPV(dataset_cfg=elpv_dataset_config)

    # Filter dataset -> use only the polycrystalline solarpanels w/ type 'poly'.
    dataset.filter(query=f"{SchemaLabelsELPV().TYPE.name}=='poly'")

    # Here comes the preprocessing step (we could e.g. make a ImageDataSetPreProcessor class/function or perhaps
    # put preprocessing methods in the ImageDataSet class itself later.
    dataset.amplify_data()

    # Specify and create a temporary directory to save our (amplified) image dataset.
    # Then open it in your OS's default filebrowser
    # Warning; can take a long time and quite a lot of storage space depending
    # on the number of samples in the dataset as well as the size of the accompanied images.
    if save_and_open_amplified_dataset:
        new_data_dir = Path(
            DIR_TMP,
            f"tmap_defectdetector_dataset_{datetime.utcnow().strftime('%Y_%m_%d_T%H%M%SZ')}",
        )
        new_data_dir.mkdir(parents=True, exist_ok=True)
        dataset.save_images(new_data_dir)
        open_directory_with_filebrowser(new_data_dir)


def main():
    os.environ["PYTHONASYNCIODEBUG"] = "1"
    version_check()
    tui()
    # example_elpv()
    # log.info("All done!")


if __name__ == "__main__":
    main()
