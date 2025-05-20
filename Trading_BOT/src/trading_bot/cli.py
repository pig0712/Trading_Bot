import time
import click

from .config import BotConfig
from .liquidation import calculate_liquidation_price
from .exchange_gateio import GateIOClient


def prompt_config() -> BotConfig:
    click.echo("📈 Gate.io 선물 분할매수 봇 설정")
    cfg = BotConfig(
        direction=click.prompt("👉 방향", type=click.Choice(["long", "short"])),
        symbol=click.prompt("👉 계약(예: BTC_USDT)", default="BTC_USDT").upper(),
        leverage=click.prompt("👉 레버리지", type=int, default=5),
        margin_mode=click.prompt("👉 마진", type=click.Choice(["cross", "isolated"])),
        entry_amount=click.prompt("👉 첫 진입(USDT)", type=float),
        max_split_count=click.prompt("👉 분할횟수", type=int),
        split_trigger_percents=[],
        split_amounts=[],
        take_profit_pct=0.0,
        stop_loss_pct=0.0,
        order_type="market",
    )
    cfg.split_trigger_percents = [
        click.prompt(f"  - {i+1}번째 퍼센트(%)", type=float)
        for i in range(cfg.max_split_count)
    ]
    cfg.split_amounts = [
        click.prompt(f"  - {i+1}번째 금액(USDT)", type=float)
        for i in range(cfg.max_split_count)
    ]
    cfg.take_profit_pct = click.prompt("👉 익절(%)", type=float)
    cfg.stop_loss_pct = click.prompt("👉 손절(%)", type=float)
    cfg.order_type = click.prompt("👉 주문방식", type=click.Choice(["market", "limit"]))
    cfg.repeat_after_take_profit = click.confirm("익절 후 반복?", default=False)
    cfg.stop_after_loss = click.confirm("손절 후 중지?", default=True)
    cfg.enable_stop_loss = click.confirm("손절 기능?", default=True)
    return cfg


def show_summary(cfg: BotConfig, price: float) -> None:
    liq, drop = calculate_liquidation_price(
        cfg.entry_amount, cfg.split_amounts, cfg.leverage, cfg.margin_mode, price
    )
    click.secho("\n📊 요약", fg="yellow")
    for k, v in cfg.to_dict().items():
        click.echo(f"{k}: {v}")
    click.echo(f"현재가      : {price:.2f} USDT")
    click.echo(f"청산가      : {liq:.2f} USDT (↓{drop:.2f}%)\n")


def run_strategy(cfg: BotConfig) -> None:
    gate = GateIOClient()
    split_idx = 0
    while True:
        price = gate.fetch_last_price(cfg.symbol)
        show_summary(cfg, price)
        if split_idx < cfg.max_split_count:
            trigger = cfg.split_trigger_percents[split_idx]
            target = price * (1 + trigger / 100)
            if (cfg.direction == "long" and price <= target) or (
                cfg.direction == "short" and price >= target
            ):
                size = int(cfg.split_amounts[split_idx] * cfg.leverage / price)
                gate.place_order(
                    cfg.symbol,
                    size=size,
                    price=None if cfg.order_type == "market" else target,
                    side=cfg.direction,
                    leverage=cfg.leverage,
                )
                split_idx += 1
        time.sleep(2)


@click.command()
def main() -> None:
    cfg = prompt_config()
    gate = GateIOClient()
    price = gate.fetch_last_price(cfg.symbol)
    show_summary(cfg, price)
    if click.confirm("실행할까요?", default=True):
        run_strategy(cfg)


if __name__ == "__main__":
    main()
