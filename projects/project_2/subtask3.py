#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
This script is for project2, subtasks 1 and 2, which aims to perform the following tasks:
 * Preprocess the ICU data
 * Resample these data when they are unbalanced
 * Scale the data
 * Fits SVMs
 * exports the results in a zip file

example usage from CLI:
 $ python3 subtask1and2.py --train_features /path/to/train_features.csv
     --train_labels /path/to/train_labels.csv
     --test_features /path/to/test_features.csv
     --predictions /path/to/predictions_subtask3.zip
     --k_fold 5
     --scaler minmax
     --model ANN
     --epochs 100

For help, run:
 $ subtask1and2.py -h

TODO:
    * Discuss next steps to improve model performance
    * Do we scale the labels?

Following Google style guide: http://google.github.io/styleguide/pyguide.html

"""

__author__ = "Josephine Yates; Philip Hartout"
__email__ = "jyates@student.ethz.ch; phartout@student.ethz.ch"

import multiprocessing
import argparse
import logging
import os
import shutil
import sys
import time

import torch

import pandas as pd
import numpy as np
from tqdm import tqdm
from sklearn.svm import SVR
from sklearn.metrics import mean_squared_error
from sklearn.model_selection import GridSearchCV, train_test_split
from sklearn.preprocessing import StandardScaler, MinMaxScaler
from torch.utils.data import Dataset, DataLoader
from torch.utils.tensorboard import SummaryWriter

IDENTIFIERS = ["pid", "Time"]
MEDICAL_TESTS = [
    "LABEL_BaseExcess",
    "LABEL_Fibrinogen",
    "LABEL_AST",
    "LABEL_Alkalinephos",
    "LABEL_Bilirubin_total",
    "LABEL_Lactate",
    "LABEL_TroponinI",
    "LABEL_SaO2",
    "LABEL_Bilirubin_direct",
    "LABEL_EtCO2",
]
VITAL_SIGNS = ["LABEL_RRate", "LABEL_ABPm", "LABEL_SpO2", "LABEL_Heartrate"]
SEPSIS = ["LABEL_Sepsis"]


def load_data():
    """Loads the preprocessed data to three different dataframes.

    Returns:
        df_train, df_train_label, df_test (pandas.core.frame.DataFrame): three dataframes
        containing the
        preprocessed training features, training labels and testing features respectively.

    """
    if FLAGS.nb_of_patients is not None:
        rows_to_load = (FLAGS.nb_of_patients * 12) + 1
    else:
        rows_to_load = None
    df_train = pd.read_csv(FLAGS.train_features, nrows=rows_to_load)
    df_train_label = pd.read_csv(FLAGS.train_labels, nrows=rows_to_load)
    df_test = pd.read_csv(FLAGS.test_features, nrows=rows_to_load)
    return df_train, df_train_label, df_test


def data_formatting(df_train, df_train_label, logger):
    """Function takes data in for formatting

    Args:
        df_train (pandas.core.DataFrame): preprocessed training features
        df_train_label (pandas.core.DataFrame): preprocessed training labels
        logger (Logger): logger

    Returns:
        X_train (np.ndarray): (n_samples, n_features) array containing features
        y_train_vital_signs (np.ndarray): (n_samples, n_features) array labels

        transform outputs later on when scaling back predictions for interpretability
    """

    # Cast to arrays
    X_train = df_train.drop(
        columns=IDENTIFIERS
    ).values
    # Create list with different label for each vital sign
    logger.info("Creating a list of labels for each vital sign")
    y_train_vital_signs = []
    for sign in VITAL_SIGNS:
        y_train_vital_signs.append(df_train_label[sign].astype(int).values)

    # Scale data to avoid convergence warning
    logger.info(f"Scaling data using {FLAGS.scaler}.")

    if FLAGS.scaler == "standard":
        scaler = StandardScaler(with_mean=True, with_std=True)
    else:
        scaler = MinMaxScaler()

    X_train = scaler.fit_transform(X_train)

    # Also transform the outputs as it may be better for the network
    # See TO DO: to we scale labels?
    vital_signs_scales = []
    # if FLAGS.model == "ANN":
    #     for i, sign in enumerate(VITAL_SIGNS):
    #         label_scaler = MinMaxScaler()
    #         y_train_vital_signs[i] = label_scaler.fit_transform(y_train_vital_signs[i].reshape(-1, 1))
    #         vital_signs_scales.append(label_scaler.scale_)
    #         y_train_vital_signs[i] = y_train_vital_signs[i].reshape(-1)

    return X_train, y_train_vital_signs


def get_vital_signs_svr_models(X_train, y_train_vital_signs, param_grid):
    """Fit SVR on given data, using gridsearch to optimize hyperparaneters

    Args:
        X_train (np.ndarray): (n_samples, n_features) array containing features
        y_train_vital_signs (list): list of np.ndarray (n_samples,) array containing labels for each
            vital sign
        param_grid (dict): dictionary containing the entries for the hyperparameter tuning.

    Returns:
        svr_models, scores (list): list of fitted models and scores for each vital sign.
    """

    svr_models = []
    scores = []
    for i, test in enumerate(VITAL_SIGNS):
        logger.info(f"Starting iteration for test {test}")
        svr = SVR()
        cores = multiprocessing.cpu_count() - 2
        tuned_svr = GridSearchCV(
            estimator=svr,
            param_grid=param_grid,
            n_jobs=cores,
            scoring="r2",
            cv=FLAGS.k_fold,
            verbose=0,
        )
        tuned_svr.fit(X_train, y_train_vital_signs[i])
        svr_models.append(tuned_svr.best_estimator_)
        scores.append(tuned_svr.best_score_)

    return svr_models, scores


def get_vital_signs_predictions(X_test, test_pids, models, device):
    """Function to obtain predictions for every model, as a confidence level : the closer to 1
    (resp 0), the more confidently) the sample belongs to class 1 (resp 0).

    Args:
        X_test (np.ndarray): array of preprocessed test values
        test_pids (np.ndarray): array of patient ids in test set
        svm_models (list): list of models for each of the medical tests

    Returns:
        df_pred (pandas.core.DataFrame): contains the predictions made by each of the models for
            their respective tests, containing for each patient id the predicted label as a confidence
            level.
    """
    df_pred = pd.DataFrame()

    for i, test in enumerate(VITAL_SIGNS):
        if FLAGS.model == "SVR":
            # Compute prediction
            y_pred = models[i].predict(X_test)
            y_mean = [
                np.mean(y_pred[i : i + 12]) for i in range(len(test_pids))
            ]
            df = pd.DataFrame({test: y_mean}, index=test_pids)
            df_pred = pd.concat([df_pred, df], axis=1)
        else:
            # Switch network to eval mode to make sure all dropout layers are there, etc..
            y_pred = (
                models[i](torch.from_numpy(X_test).to(device).float())
                .cpu()
                .detach()
                .numpy()
            )
            y_mean = [
                np.mean(y_pred[i : i + 12]) for i in range(len(test_pids))
            ]
            df = pd.DataFrame({test: y_mean}, index=test_pids)
            df_pred = pd.concat([df_pred, df], axis=1)
    return df_pred


def convert_to_cuda_tensor(X_train, X_test, y_train, y_test, device):
    """Converts a number of np.ndarrays to tensors placed on the device specified.

    Args:
        X_train (np.ndarray): (n_samples, n_features) array containing training features
        X_test (np.ndarray): (n_samples, n_features) array containing testing features
        y_train (np.ndarray): (n_samples,) array containing training labels
        y_test (np.ndarray): (n_samples,) array containing testing labels
        device (torch.device): device on which the tensors should be placed (CPU/CUDA GPU)

    Returns:

    """
    return (
        torch.from_numpy(X_train).to(device).float(),
        torch.from_numpy(X_test).to(device).float(),
        torch.from_numpy(y_train).to(device).float(),
        torch.from_numpy(y_test).to(device).float(),
    )


class Feedforward(torch.nn.Module):
    """ Definition of the feedfoward neural network. It currently has three layers which can be
    modified in the function where the network is trained.
    """

    def __init__(self, input_size, hidden_size, p=0.2):
        super(Feedforward, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.dropout = torch.nn.Dropout(p=p)
        self.fc1 = torch.nn.Linear(self.input_size, self.hidden_size)
        torch.nn.init.xavier_normal_(self.fc1.weight)
        self.bn = torch.nn.BatchNorm1d(hidden_size)
        self.relu = torch.nn.ReLU()
        self.fc2 = torch.nn.Linear(self.hidden_size, hidden_size)
        torch.nn.init.xavier_normal_(self.fc2.weight)
        self.fc3 = torch.nn.Linear(self.hidden_size, 1)
        torch.nn.init.xavier_normal_(self.fc3.weight)
        self.sigmoid = torch.nn.Sigmoid()

    def forward(self, x):
        """Function where the forward pass is defined. The backward pass is deternmined by the
            autograd function built into PyTorch.

        Args:
            x (torch.Tensor): Tensor (n_samples,n_features) tensor containing training input
                features

        Returns:
            output (torch.Tensor): (n_samples,n_features) tensor containing
                the predicted output for each sample.
        """
        hidden = self.fc1(x)
        hidden_bn = self.bn(hidden)
        relu = self.relu(hidden_bn)
        hidden_2 = self.dropout(self.fc2(relu))
        hidden_2_bn = self.bn(hidden_2)
        relu_2 = self.relu(hidden_2_bn)
        output = self.dropout(self.fc3(relu_2))
        output = self.relu(output)
        return output


class Data(Dataset):
    """ Class used to load the data in minibatches to control the neural network stability during
        training.
    """

    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.len = self.x.shape[0]

    def __getitem__(self, index):
        return self.x[index], self.y[index]

    def __len__(self):
        return self.len


def get_vital_signs_ann_models(x_input, y_input, logger, device):
    """Main function to train the neural networks for the data.

    Args:
        x_input (np.ndarray): (n_samples,n_features) array containing the training features
        y_input (np.ndarray): (n_samples,) array containing the training labels
        logger (Logger): logger
        device (torch.device): device on which the tensors should be placed (CPU/CUDA GPU)

    Returns:
        ann_models (list): list of trained feedforward neural networks
        scores (list): list of the testing scores of the trained feedforward neural networks
    """
    logger.info(f"Using {device} to train the neural network.")
    ann_models = []
    scores = []
    for i, sign in enumerate(VITAL_SIGNS):
        logger.info(f"Starting neural network training for vital sign {sign}")
        X_train, X_test, y_train, y_test = train_test_split(
            x_input, y_input[i], test_size=0.10, random_state=42, shuffle=True
        )
        logger.info("Converting arrays to tensors")
        X_train_tensor, X_test_tensor, y_train_tensor, y_test_tensor = convert_to_cuda_tensor(
            X_train, X_test, y_train, y_test, device
        )
        model = Feedforward(35, 150, 0.0)
        criterion = torch.nn.MSELoss()
        optimizer = torch.optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
        dataset = Data(X_train_tensor, y_train_tensor)
        batch_size = 2048  # Ideally we want any multiple of 12 here
        trainloader = DataLoader(dataset=dataset, batch_size=batch_size)
        LOSS = []
        if torch.cuda.is_available():
            model.cuda()
        model.float()

        logger.info("Removing data from previous run")
        dirpath = 'runs'
        if os.path.exists(dirpath) and os.path.isdir(dirpath):
            shutil.rmtree(dirpath)

        logger.info("Commencing neural network training")
        writer = SummaryWriter()
        for epoch in tqdm(list(range(FLAGS.epochs))):
            for x, y in trainloader:
                yhat = model(x)
                loss = criterion(yhat.float(), y.reshape((y.shape[0], 1)))
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            LOSS.append(loss)
            writer.add_scalar("Training_loss", loss, epoch)
        writer.close()

        X_test_tensor = X_test_tensor.to(device)
        y_pred = model(X_test_tensor).cpu().detach().numpy()
        logger.info(f"Value of the test sample if {y_test} and value for the predicted "
                    f"sample is {y_pred}")
        test_error = mean_squared_error(y_test, y_pred)
        logger.info(f"MSE for test set is {test_error}")
        logger.info(f"Finished test for vital sign {sign}")
        ann_models.append(model)
        scores.append(test_error)
    return ann_models, scores


def main(logger):
    """Primary function reading, preprocessing and modelling the data

    Args:
        logger (Logger): logger to get information about the status of the script when running

    Returns:
        None
    """

    logger.info("Loading data")
    df_train, df_train_label, df_test = load_data()
    logger.info("Finished Loading data")

    X_train, y_train_vital_signs = data_formatting(
        df_train, df_train_label, logger
    )

    logger.info("Beginning modelling process.")

    if FLAGS.model == "SVR":
        device=None
        # train model here
        param_grid = {
            "kernel": ["linear", "poly", "rbf", "sigmoid"],
            "degree": np.arange(1, 4, 1),
            "gamma": np.linspace(0.1, 10, num=3),
            "coef0": [0],
            "tol": [0.001],
            "C": np.linspace(0.1, 10, num=3),
            "epsilon": [0.1],
            "shrinking": [True],
            "cache_size": [1000],
            "verbose": [2],
            "max_iter": [1000],
        }
        vital_signs_models, vital_signs_models_scores = get_vital_signs_svr_models(
            X_train, y_train_vital_signs, param_grid
        )
    else:
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        vital_signs_models, scores = get_vital_signs_ann_models(
            X_train, y_train_vital_signs, logger, device
        )

    # get the unique test ids of patients
    test_pids = np.unique(df_test[IDENTIFIERS].values)
    logger.info("Fetch predictions.")
    X_test = df_test.drop(columns=IDENTIFIERS).values
    vital_signs_predictions = get_vital_signs_predictions(
        X_test, test_pids, vital_signs_models, device
    )

    logger.info("Export predictions DataFrame to a zip file")
    # Export pandas dataframe to zip archive.
    vital_signs_predictions.to_csv(
        FLAGS.predictions, index=False, float_format="%.3f", compression="zip"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="CLI args for folder and file \
    directories as well as model options"
    )

    parser.add_argument(
        "--train_features",
        "-train_f",
        type=str,
        required=True,
        help="path to the CSV file containing the preprocessed training \
      features",
    )

    parser.add_argument(
        "--test_features",
        "-test",
        type=str,
        required=True,
        help="path to the CSV file containing the preprocessed testing \
 features",
    )

    parser.add_argument(
        "--train_labels",
        "-train_l",
        type=str,
        required=True,
        help="path to the CSV file containing the preprocessed training \
     labels",
    )

    parser.add_argument(
        "--predictions",
        "-pred",
        type=str,
        required=True,
        help="path to the zip file containing the \
     predictions",
    )

    parser.add_argument(
        "--nb_of_patients",
        "-nb_pat",
        type=int,
        required=False,
        help="Number of patients to consider in run. If not specified, then consider all patients",
    )

    parser.add_argument(
        "--scaler",
        "-scale",
        type=str,
        required=True,
        help="Scaler to be used to transform the data.",
        choices=["minmax", "standard"],
    )

    parser.add_argument(
        "--k_fold",
        "-k",
        type=int,
        required=False,
        help="k to perform k-fold cv in the gridsearch",
    )

    parser.add_argument(
        "--model",
        "-m",
        type=str,
        required=True,
        help="whether to train a neural network or an SVR for the tasks",
        choices=["SVR", "ANN"],
    )

    parser.add_argument(
        "--epochs", "-ep", type=int, required=False, help="", default=100
    )

    FLAGS = parser.parse_args()

    # clear logger.
    logging.basicConfig(
        level=logging.DEBUG, filename="script_status_subtask3.log"
    )

    logger = logging.getLogger("IML-P2-T3")

    # Create a second stream handler for logging to `stderr`, but set
    # its log level to be a little bit smaller such that we only have
    # informative messages
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)

    # Use the default format; since we do not adjust the logger before,
    # this is all right.
    stream_handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(levelname)s [%(name)s.%(funcName)s:%(lineno)d] %(message)s"
        )
    )
    logger.addHandler(stream_handler)

    logger.info("Usage:\n{0}\n".format(" ".join([x for x in sys.argv])))
    logger.info("All settings used:")
    for k, v in sorted(vars(FLAGS).items()):
        logger.info("{0}: {1}".format(k, v))

    main(logger)
