# Changelog

## 2

- Any number of X accounts: `HANDLES` takes one or more. The duty reads up to 5 accounts per review and rotates through the rest, oldest read first, so every account is read in turn and the X search quota holds; coverage and status say so.
- The basket is symbol, chain and weight, with no address. Buys go by symbol and chain; the contract is learned from the fill and used for sales. Native coins (ETH, BNB, SOL...) are fine as tickers. Market data comes from the verified token-search row on the chain until the first fill; a token with no market row is valued from the wallet and the model is told its data is missing.
- A v1 duty keeps its v1 code: to take more than 5 accounts or an address-free basket, re-file it with `x-portfolio@2`; new settings alone are not enough.
- Gas coins are matched by chain (ETH on 1, 8453, 42161, 4663; BNB on 56; SOL); every other token's address is learned from the fill or its pocket row, and a sale waits until it is known.
- Forks of the code are checked on changed numeric constants against the owner's words (settings lint R5); every tuning constant is now a module-level literal.

## 1

- First release: persistent worldview, owner-confirmed basket, model-proposed weights guarded by code, hourly timer. Portfolio-manager reasoning: account track record, market cross-checks, categories with indirect and macro evidence, reviews without new posts, market-verified invalidation exits.
