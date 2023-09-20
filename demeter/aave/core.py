from _decimal import Decimal
from typing import Dict

from demeter import DECIMAL_0, TokenInfo
from demeter.aave._typing import SupplyKey, ActionKey, BorrowKey


class AaveV3CoreLib(object):
    pass

    SECONDS_IN_A_YEAR = 31536000

    @staticmethod
    def rate_to_apy(rate: Decimal) -> Decimal:
        return (1 + rate / AaveV3CoreLib.SECONDS_IN_A_YEAR) ** AaveV3CoreLib.SECONDS_IN_A_YEAR - 1

    @staticmethod
    def get_current_amount(net_value_in_pool: Decimal, current_liquidity_rate: Decimal) -> Decimal:
        return Decimal(net_value_in_pool) * Decimal(current_liquidity_rate)

    @staticmethod
    def get_base_amount(amount: Decimal, pool_liquidity_rate: Decimal) -> Decimal:
        return amount / pool_liquidity_rate

    @staticmethod
    def health_factor(supplies: Dict[SupplyKey, Decimal], borrows: Dict[BorrowKey, Decimal], risk_parameters):
        # (all supplies * liqThereshold) / all borrows
        a = sum([s * risk_parameters.loc[key.token.name].liqThereshold for key, s in supplies.items()])
        b = sum(borrows.values())
        return AaveV3CoreLib.safe_div(a, b)

    @staticmethod
    def current_ltv(supplies: Dict[SupplyKey, Decimal], risk_parameters):
        all_supplies = DECIMAL_0
        for t, s in supplies.items():
            all_supplies += s * risk_parameters.loc[t.token.name].LTV

        amount = sum(supplies.values())
        return AaveV3CoreLib.safe_div(all_supplies, amount)

    @staticmethod
    def total_liquidation_threshold(supplies: Dict[SupplyKey, Decimal], risk_parameters):
        # (token_amount0 * LT0 + token_amount1 * LT1 + ...) / (token_amount0 + token_amount1)

        sum_amount = DECIMAL_0
        rate = DECIMAL_0
        for t, s in supplies.items():
            sum_amount += s
            rate += s * risk_parameters.loc[t.token.name].liqThereshold

        return AaveV3CoreLib.safe_div(rate, sum_amount)

    @staticmethod
    def safe_div(a: Decimal, b: Decimal) -> Decimal:
        if b == 0:
            return Decimal(0)  # Consistent with the contract
        else:
            return a / b

    @staticmethod
    def get_apy(amounts: Dict[ActionKey, Decimal], rate_dict: Dict[TokenInfo, Decimal]):
        a = sum([amounts[key] * AaveV3CoreLib.rate_to_apy(rate_dict[key.token]) for key, amount in amounts.items()])
        b = sum(amounts.values())
        return AaveV3CoreLib.safe_div(a, b)