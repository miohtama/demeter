import json
import os
from _decimal import Decimal
from datetime import date, timedelta
from typing import List, Dict, Tuple

import pandas as pd

from ._typing import (
    DeribitOptionMarketDescription,
    DeribitTokenConfig,
    DeribitMarketStatus,
    OptionMarketBalance,
    OptionPosition,
    OptionKind,
    BuyAction,
    InstrumentStatus,
    Order,
    SellAction,
    ExpiredAction,
    DeliverAction,
)
from .helper import round_decimal
from .. import TokenInfo
from .._typing import DemeterError
from ..broker import Market, MarketInfo, write_func
from ..utils import float_param_formatter

DEFAULT_DATA_PATH = "./data"


def order_converter(array_str) -> List:
    return json.loads(array_str)


class DeribitOptionMarket(Market):
    """
    You can backtest as a taker only.
    """

    def __init__(
        self,
        market_info: MarketInfo,
        token: TokenInfo,
        data: pd.DataFrame = None,
        data_path: str = DEFAULT_DATA_PATH,
    ):
        super().__init__(market_info=market_info, data_path=data_path, data=data)
        self.token: TokenInfo = token
        self.token_config: DeribitTokenConfig = DeribitOptionMarket.TOKEN_CONFIGS[token]
        self.positions: Dict[str, OptionPosition] = {}
        self.decimal = self.token_config.min_fee_decimal

    MAX_FEE_RATE = Decimal("0.125")
    ETH = TokenInfo("eth", 18)
    BTC = TokenInfo("btc", 8)
    TOKEN_CONFIGS = {
        ETH: DeribitTokenConfig(
            trade_fee_rate=Decimal("0.0003"),
            delivery_fee_rate=Decimal("0.00015"),
            min_trade_decimal=0,
            min_fee_decimal=-6,
        ),
        BTC: DeribitTokenConfig(
            trade_fee_rate=Decimal("0.0003"),
            delivery_fee_rate=Decimal("0.00015"),
            min_trade_decimal=-1,
            min_fee_decimal=-8,
        ),
    }

    def __str__(self):
        return json.dumps(self.description()._asdict())

    def description(self):
        """
        Get a brief description of this market
        """
        return DeribitOptionMarketDescription(type(self).__name__, self._market_info.name, len(self.positions))

    def load_data(self, start_date: date, end_date: date):
        """
        Load data from folder set in data_path. Those data file should be downloaded by demeter, and meet name rule.
        Deribit-option-book-{token}-{day.strftime('%Y%m%d')}.csv

        :param start_date: start day
        :type start_date: date
        :param end_date: end day, the end day will be included
        :type end_date: date
        """
        self.logger.info(f"start load files from {start_date} to {end_date}...")
        day = start_date
        df = pd.DataFrame()

        while day <= end_date:
            path = os.path.join(
                self.data_path,
                f"Deribit-option-book-{self.token.name}-{day.strftime('%Y%m%d')}.csv",
            )
            if not os.path.exists(path):
                raise IOError(f"resource file {path} not found")

            day_df = pd.read_csv(
                path,
                parse_dates=["time", "expiry_time"],
                index_col=["time", "instrument_name"],
                converters={"asks": order_converter, "bids": order_converter},
            )
            day_df.drop(columns=["actual_time", "min_price", "max_price"], inplace=True)
            df = pd.concat([df, day_df])
            day += timedelta(days=1)

        self._data = df
        self.logger.info("data has been prepared")

    def check_market(self):
        """
        check market before back test
        """
        if not isinstance(self.data, pd.DataFrame):
            raise DemeterError("data must be type of data frame")

    def set_market_status(
        self,
        data: DeribitMarketStatus,
        price: pd.Series,
    ):
        """
        Set up market status, such as liquidity, price

        :param data: market status
        :type data: Series | MarketStatus
        :param price: current price
        :type price: Series

        """
        super().set_market_status(data, price)
        if data.data is None:
            data.data = self._data.loc[data.timestamp]
        self._market_status = data

    # region for option market only

    def get_trade_fee(self, amount: Decimal, total_premium: Decimal) -> Decimal:
        """
        https://www.deribit.com/kb/fees
        """
        return round_decimal(
            min(self.token_config.trade_fee_rate * amount, DeribitOptionMarket.MAX_FEE_RATE * total_premium),
            self.decimal,
        )

    def get_price_from_data(self):
        if self._data is None:
            raise DemeterError("data is empty")
        price = []
        for hour, hour_df in self._data.groupby(level=0):
            price.append({"time": hour, self.token.name: hour_df.iloc[0]["underlying_price"]})
        price_df = pd.DataFrame(price)
        price_df.set_index(["time"], inplace=True)
        return price_df

    def get_deliver_fee(self, amount: Decimal, total_premium: Decimal):
        """
        https://www.deribit.com/kb/fees
        """
        return round_decimal(
            min(self.token_config.delivery_fee_rate * amount, DeribitOptionMarket.MAX_FEE_RATE * total_premium),
            self.decimal,
        )

    def __get_trade_amount(self, amount: Decimal):
        if amount < self.token_config.min_amount:
            return self.token_config.min_amount
        return round_decimal(amount, self.token_config.min_trade_decimal)

    @write_func
    @float_param_formatter
    def buy(
        self,
        instrument_name: str,
        amount: float | Decimal,
        price_in_token: float | Decimal | None = None,
        price_in_usd: float | Decimal | None = None,
    ) -> List[Order]:
        """
        if price is not none, will set at that price
        or else will buy according to bids

        """
        amount, instrument, price_in_token = self._check_transaction(
            instrument_name, amount, price_in_token, price_in_usd, True
        )

        # deduct bids amount
        asks = instrument.asks
        ask_list = self._deduct_order_amount(amount, asks, price_in_token)

        # write positions back
        if self.data is not None:
            self.data.loc[(self._market_status.timestamp, instrument_name), "asks"] = asks

        total_premium = Decimal(sum([Decimal(t.amount) * Decimal(t.price) for t in ask_list]))
        fee_amount = self.get_trade_fee(amount, total_premium)
        self.broker.subtract_from_balance(self.token, total_premium + fee_amount)

        # add position
        average_price = Order.get_average_price(ask_list)
        if instrument_name not in self.positions.keys():
            self.positions[instrument_name] = OptionPosition(
                instrument_name=instrument_name,
                expiry_time=instrument.expiry_time,
                strike_price=instrument.strike_price,
                type=OptionKind(instrument.type),
                amount=amount,
                avg_buy_price=average_price,
                buy_amount=amount,
                avg_sell_price=Decimal(0),
                sell_amount=Decimal(0),
            )
        else:
            position = self.positions[instrument_name]
            position.avg_buy_price = Order.get_average_price(
                [Order(average_price, amount), Order(position.avg_buy_price, position.buy_amount)]
            )
            position.buy_amount += amount
            position.amount += amount

        self._record_action(
            BuyAction(
                market=self._market_info,
                instrument_name=instrument_name,
                type=OptionKind(instrument.type),
                average_price=average_price,
                amount=amount,
                total_premium=total_premium,
                mark_price=Decimal(instrument.mark_price),
                underlying_price=Decimal(instrument.underlying_price),
                fee=fee_amount,
                orders=ask_list,
            )
        )
        return ask_list

    @staticmethod
    def _find_available_orders(price, order_list) -> List:
        error = Decimal("0.001")
        return list(filter(lambda x: (1 - error) * price < x[0] < (1 + error) * price, order_list))

    @write_func
    @float_param_formatter
    def sell(
        self,
        instrument_name: str,
        amount: float | Decimal,
        price_in_token: float | Decimal | None = None,
        price_in_usd: float | Decimal | None = None,
    ) -> List[Order]:
        amount, instrument, price_in_token = self._check_transaction(
            instrument_name, amount, price_in_token, price_in_usd, False
        )

        # deduct  amount
        bids = instrument.bids
        bid_list = self._deduct_order_amount(amount, bids, price_in_token)

        # write positions back
        if self.data is not None:
            self.data.loc[(self._market_status.timestamp, instrument_name), "bids"] = bids

        total_premium = Decimal(sum([Decimal(t.amount) * Decimal(t.price) for t in bid_list]))
        fee = self.get_trade_fee(amount, total_premium)
        self.broker.add_to_balance(self.token, total_premium - fee)

        # subtract position
        average_price = Order.get_average_price(bid_list)
        if instrument_name not in self.positions.keys():
            raise DemeterError("No such instrument position")

        position = self.positions[instrument_name]
        position.avg_sell_price = Order.get_average_price(
            [Order(average_price, amount), Order(position.avg_sell_price, position.sell_amount)]
        )
        position.sell_amount += amount
        position.amount -= amount

        if position.amount <= Decimal(0):
            del self.positions[instrument_name]

        self._record_action(
            SellAction(
                market=self._market_info,
                instrument_name=instrument_name,
                type=OptionKind(instrument.type),
                average_price=average_price,
                amount=amount,
                total_premium=total_premium,
                mark_price=Decimal(instrument.mark_price),
                underlying_price=Decimal(instrument.underlying_price),
                fee=fee,
                orders=bid_list,
            )
        )
        return bid_list

    def _deduct_order_amount(self, amount, orders, price_in_token):
        order_list = []
        if price_in_token is not None:
            for order in orders:
                if price_in_token == Decimal(str(order[0])):
                    order[1] -= amount
                    order_list.append(Order(price_in_token, amount))
        else:
            amount_to_deduct = amount
            for order in orders:
                if order[1] == 0 or order[1] == Decimal(0):
                    continue
                should_deduct = min(Decimal(str(order[1])), amount_to_deduct)
                amount_to_deduct -= should_deduct
                order[1] -= should_deduct
                order_list.append(Order(Decimal(str(order[0])), should_deduct))
                if order[1] > 0 or amount_to_deduct == Decimal(0):
                    break
        return order_list

    def _check_transaction(self, instrument_name, amount, price_in_token, price_in_usd, is_buy):
        if instrument_name not in self._market_status.data.index:
            raise DemeterError(f"{instrument_name} is not in current orderbook")
        instrument: InstrumentStatus = self._market_status.data.loc[instrument_name]
        if instrument.state != "open":
            raise DemeterError(f"state of {instrument_name} is not open")
        if amount < self.token_config.min_amount:
            raise DemeterError(
                f"amount should greater than min amount {self.token_config.min_amount} {self.token.name}"
            )
        amount = self.__get_trade_amount(amount)
        if price_in_usd is not None and price_in_token is None:
            price_in_token = price_in_usd / instrument.underlying_price

        if is_buy:
            available_orders = instrument.asks
        else:
            available_orders = instrument.bids

        if price_in_token is not None:
            # to prevent error in decimal
            available_orders = DeribitOptionMarket._find_available_orders(price_in_token, available_orders)

            if len(available_orders) < 1:
                raise DemeterError(
                    f"{instrument_name} doesn't have a order in price {price_in_token} {self.token.name}"
                )
            price_in_token = Decimal(str(available_orders[0][0]))
            available_amount = Decimal(available_orders[0][1])
        else:
            available_amount = sum([Decimal(x[1]) for x in available_orders])
        if amount > available_amount:
            raise DemeterError(
                f"insufficient order to buy, required amount is {amount}, available amount is {available_amount}"
            )

        return amount, instrument, price_in_token

    def update(self):
        self.check_option_exercise()

    def get_market_balance(self, prices: pd.Series | Dict[str, Decimal] = None) -> OptionMarketBalance:
        """
        Get market asset balance, such as current positions, net values

        :param prices: current price of each token
        :type prices: pd.Series | Dict[str, Decimal]
        :return: Balance in this market includes net value, position value
        :rtype: MarketBalance
        """
        put_count = len(list(filter(lambda x: x.type == OptionKind.put, self.positions.values())))
        call_count = len(list(filter(lambda x: x.type == OptionKind.call, self.positions.values())))

        total_premium = Decimal(0)
        delta = gamma = Decimal(0)
        for position in self.positions.values():
            instr_status = self.market_status.data.loc[position.instrument_name]
            instrument_premium = position.amount * round_decimal(instr_status.mark_price, self.decimal)
            total_premium += instrument_premium
            delta += instrument_premium * round_decimal(instr_status.delta, self.decimal)
            gamma += instrument_premium * round_decimal(instr_status.gamma, self.decimal)

        delta = Decimal(0) if total_premium == Decimal(0) else delta / total_premium
        gamma = Decimal(0) if total_premium == Decimal(0) else gamma / total_premium

        return OptionMarketBalance(total_premium, put_count, call_count, delta, gamma)

    # region exercise
    @write_func
    def check_option_exercise(self):
        """
        loop all the option position,
        if expired, if option position is in the money, then exercise.
        if out of the money, then abandon
        """
        key_to_remove = []
        for pos_key, position in self.positions.items():
            if self._market_status.timestamp >= position.expiry_time:
                # should
                if position.instrument_name not in self._market_status.data.index:
                    raise DemeterError(f"{position.instrument_name} is not in current orderbook")
                instrument: InstrumentStatus = self.market_status.data.loc[position.instrument_name]
                deliver_amount = deliver_fee = None
                if position.type == OptionKind.put and position.strike_price > instrument.underlying_price:
                    deliver_amount, deliver_fee = self._deliver_option(position, instrument, False)
                elif position.type == OptionKind.call and position.strike_price < instrument.underlying_price:
                    deliver_amount, deliver_fee = self._deliver_option(position, instrument, True)
                if deliver_amount is not None:
                    self._record_action(
                        DeliverAction(
                            market=self._market_info,
                            instrument_name=pos_key,
                            type=position.type,
                            mark_price=round_decimal(instrument.mark_price, self.decimal),
                            amount=position.amount,
                            total_premium=position.amount * round_decimal(instrument.mark_price, self.decimal),
                            strike_price=position.strike_price,
                            underlying_price=round_decimal(instrument.underlying_price, self.decimal),
                            deriver_amount=deliver_amount,
                            fee=deliver_fee,
                            income_amount=deliver_amount - deliver_fee,
                        )
                    )
                key_to_remove.append(pos_key)

        for pos_key in key_to_remove:
            position = self.positions[pos_key]
            if pos_key in self.market_status.data.index:
                instrument: InstrumentStatus = self.market_status.data.loc[position.instrument_name]
            else:
                instrument = InstrumentStatus(mark_price=0, underlying_price=0)
            self._record_action(
                ExpiredAction(
                    market=self._market_info,
                    instrument_name=pos_key,
                    type=position.type,
                    mark_price=round_decimal(instrument.mark_price, self.decimal),
                    amount=position.amount,
                    total_premium=position.amount * round_decimal(instrument.mark_price, self.decimal),
                    strike_price=position.strike_price,
                    underlying_price=round_decimal(instrument.underlying_price, self.decimal),
                )
            )
            del self.positions[pos_key]

    def _deliver_option(
        self, option_pos, instrument: InstrumentStatus, is_call
    ) -> Tuple[Decimal | None, Decimal | None]:
        fee = self.get_deliver_fee(
            option_pos.amount, option_pos.amount * round_decimal(instrument.mark_price, self.decimal)
        )
        if is_call:
            price_diff = instrument.underlying_price - option_pos.strike_price
        else:
            price_diff = option_pos.strike_price - instrument.underlying_price

        balance_to_add = round_decimal(
            option_pos.amount * Decimal(price_diff / instrument.underlying_price), self.decimal
        )
        if balance_to_add <= fee:
            return None, None
        self.broker.add_to_balance(self.token, balance_to_add - fee)
        return balance_to_add, fee

    # endregion

    # endregion
