from dataclasses import asdict, dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ParsedTransaction:
    booking_date: str
    value_date: str
    booking_type: str
    counterparty: str
    description: str
    amount: str
    currency: str
    source_page: int

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ParsedStatement:
    transactions: list[ParsedTransaction]
    opening_balance: Decimal | None
    closing_balance: Decimal | None

    @property
    def transaction_total(self):
        return sum((Decimal(item.amount) for item in self.transactions), Decimal("0.00"))

    @property
    def reconciliation_difference(self):
        if self.opening_balance is None or self.closing_balance is None:
            return None
        return (self.opening_balance + self.transaction_total - self.closing_balance).quantize(
            Decimal("0.01")
        )
