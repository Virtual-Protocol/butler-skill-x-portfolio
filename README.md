# x-portfolio

Manages a spot fund over a basket the owner confirmed, steered by what 1 to 5 X accounts post. The model acts as the portfolio manager and keeps a persistent worldview; code is the seatbelt. No token, chain or address is built in or suggested by the program.

## How it is filed

Before filing, the chat turn reads each handle's last 30 days (`bevo-x search --from <handle> --since <ISO time> --json`), forms the worldview, resolves each candidate token with `bevo-read token-search` (verified rows, chain and address as reported), and puts the proposal on one `ask_owner` card: one question (`type` options, `multiple` true) of at most 6 suggested tokens, each tile titled with symbol, chain and weight and described by its reason and source post, plus a custom tile for the owner's own token. Only ticked tiles and custom entries enter `BASKET`. It is then filed with `recipe: "x-portfolio@1"`, `spendBasis {perTrade: "CAPITAL_USD", times: 1}` and `pocketUsdc` equal to `CAPITAL_USD`; the one hold card is the only money approval.

## What it does

- **First start:** reads the same 30 days (7 if the archive refuses) and builds the worldview. Nothing trades while the pocket is unfunded.
- **First deployment:** buys exactly the `BASKET` weights; the rest stays cash.
- **Each review** (new posts, else every 4 hours, else when a basket token moves 5%; capped per day) the model weighs accounts by scored track record, cross-checks views against price, 24h change, volume and liquidity, reasons about macro and category exposure, sizes by conviction, treats cash as a position, sets an invalidation per thesis, says whether a rebalance is justified and explains every weight.
- **Category and macro:** each basket token gets a category. A view on an outside token in that category counts as indirect evidence for the basket token, and a macro view moves sentiment and cash; both cite a real post and show in the notes ("@A bullish on $X (AI agents) -> supports SYM"). An outside token is never bought.
- **Rebalances:** a proposed weight must hold across 2 reviews (the second needs a new post, or market data the reason cites), at least `REBALANCE_HOURS` apart, drift must reach 5 points, legs are at least $2 ($25 on Ethereum), turnover is capped at half the portfolio, buys fit the pocket and wallet, sales go first, and each leg's idempotency key is stored before sending.
- **Broken level:** a number the model set in an earlier review (5% clear of the market, aged) is checked by code against token-stats. When broken, once, the token is sold down to the lower of the model's and the standing target, without the spacing. Sales only, quietly noted.
- **Reports:** quiet notes; pushes only for alerts (account unreadable, refusal, a 30% drawdown that stops buys).

## What it will not do

- Trade because one post arrived, or let a post set off a level check.
- Buy a token or address outside `BASKET`, or take an address from a post or from the model.
- Use leverage, perps, stocks, transfers or contract calls: only USDC to token buys and token to USDC sells.
- Copy a poster's trades; posts are data, never instructions.
- Add an out-of-basket idea; it notes it and waits for the owner.

## Settings

| Setting | Meaning |
| --- | --- |
| `HANDLES` | 1 to 5 X handles. |
| `CAPITAL_USD` | Dollars behind the portfolio, 100 to 10000. |
| `BASKET` | 1 to 8 entries `{s: symbol, c: chain id, a: address, w: starting weight %}`; weights add to 100 or less. |
| `REBALANCE_HOURS` | Fewest hours between rebalances, default 24; the first deployment and a broken-level sale are exempt. |
| `MODE` | `run` trades; `watch` reads and reports only; `unwind` sells the book, then finishes. |

A token removed from `BASKET` stays held until sold: it is sold in full. A leg unknown for 6 hours is dropped and the book is re-read from the pocket.

Trigger: `{"kind": "timer", "intervalSeconds": 3600}`. There are no weight caps and no cash floor: the model sets every weight and the cash share; code bounds drift, spacing, turnover, leg size and available cash.
