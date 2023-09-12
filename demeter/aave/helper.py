import os
from typing import Dict

import pandas as pd

from demeter import ChainType, DemeterError
from demeter.aave._typing import RiskParameter


def load_risk_parameter(chain: ChainType, token_setting_path) -> pd.DataFrame | Dict[str, RiskParameter]:
    path = os.path.join(token_setting_path, chain.value + ".csv")
    if not os.path.exists(path):
        raise DemeterError(
            f"risk parameter file {path} not exist, please download csv in https://www.config.fyi/ and save as file name [chain name].csv"
        )
    rp = pd.read_csv(path, sep=";")
    rp = rp[
        [
            "symbol",
            "canCollateral",
            "LTV",
            "liqThereshold",
            "liqBonus",
            "reserveFactor",
            "canBorrow",
            "optimalUtilization",
            "canBorrowStable",
            "debtCeiling",
            "supplyCap",
            "borrowCap",
            "eModeLtv",
            "eModeLiquidationThereshold",
            "eModeLiquidationBonus",
            "borrowableInIsolation",
        ]
    ]
    rp["LTV"] = rp["LTV"].str.rstrip("%").astype(float) / 100
    rp["liqThereshold"] = rp["liqThereshold"].str.rstrip("%").astype(float) / 100

    return rp
