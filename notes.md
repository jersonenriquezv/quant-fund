### Inducement ###

How to identifiy potenial zones to buy/sell? 

Area that draws you into a trade before stoopping you out before moving your overall given directions.

Example: Uptrend market where there are "two zones" or maybe even three and you dont know which zone to pick. Many times if you buy from zone 1 the market go down and then trade through that level filling zone 2 before moving in your direction = losing trade

1. Liquidity: 

Generally EQH or EQL (equal highs, equal lows) levels in the market where there are a bunch of orders accumulating. The market reacts from areas where orders are setting. so it is very common that the price breaks eql or eql before continue in the given direction. 
     The easiest way to identify potencial inducement zones is to look if we have points of liquidity underneath the zone that sits above the second zone. 
     If we have eql = high probability that the market is magnetized under this level at least. Therefore making zone 1 in this instance very low probability to buy from and we should instead look to take trades for zone 2 once the liquidity from zone 1 has been taking out. 
Inducements not always are very clear. 
     There are some thing we can do: 
          Where the strenght on buying and selling movements has came from. 
          Example: BoS took place from an 
          area of demand. We saw demand acumulation and then a big rally to the upside which create the Bos. This could tell us that zone 2 has the strenght to break strucutre an either continue the trend or potentially reverse the trend. 
          zone 2 where the buying took place to BoS now it is a zone where institutional presence has took place. == significant amount of buying interest in that level.
          Quite likely that the price trade again that zone.
          - it changes when there is an impulse but not enought to bos. but a little correction before bos... we can consider that the significant buying is a little bit above of zone two. on that small correction or reaccumulation.. zone 1 and the market may not go to zone 2 again. 


## Confirmation entries ###
It is an entry model used to confirm a shift in market trend before placing a trade. For a bullish setup, wait for the market to print a Break of Structure (BOS) where a downtrend of lower highs shifts into higher highs
 1. Once the shift is confirmed with a candle closure (not just a wick), look for a pullback into a demand zone (the last candle before the impulse move). You can then place a buy limit order at this zone 
2. Place your stop loss just below the significant swing low of the confirmation entry. If the market breaks this level, the trade is invalidated, allowing you to minimize losses quickly

Reversal entries or continuation

Confirmation entries are most effective when they align with the larger trend Always consider the broader market context to increase your win rate.


Reversal Trades
Identification: A reversal occurs when the market is transitioning from a clear downtrend (printing lower lows and lower highs) directly into an uptrend, or vice versa. It marks the end of a previous trend and the start of a new one. 
Trading Strategy: Since there may not be significant historical demand or supply zones to lean on, the entry is based on the Break of Structure (BOS) itself. You wait for a candle closure above a previous lower high to confirm the shift, then look to place a limit order at the demand/supply zone that initiated the move. Because you are catching the beginning of a new trend, the speaker suggests that targets can often be set at higher, more ambitious levels. (10:01 - 11:57)
Continuation Trades
Identification: A continuation trade occurs when an uptrend (or downtrend) is already well-established. Instead of waiting for a total reversal, you look for a temporary 'corrective' pullback within that ongoing trend. 
Trading Strategy: The goal is to join the existing trend in line with the dominant market momentum. You wait for the price to pull back into a demand zone (the last candle before an impulse). Once the price taps into this zone and shows signs of respecting the trend, you enter the trade. These are considered high-probability trades because they align with the overall, established direction of the market. 

Wickoff Theory

THeory built around supply & demand. 
Supply = units sold to market
demand = willigness of buyers to purcharse
More supply = lower prices
More demand = high prices


Wickoff suggest markets move in a constant cycle of HSLD -> depreciation
                                                    HDLS -> appreciation 

4 phase we can use to track s&d 
1. Accumulaiton: 
Phase where buyers accumulate orders
2. Markup:
Buyers take control completely -> price appreciation takes place
3. Distribution: 
Sellers are starting  followed by a drive to the downside
4. Markdown:
Sellers take control competely -> price depreciaition 

Market only move in this 4 phases according to wyckoff

Buying  & slling is a battle 
-More agggresive buying than selling (demand) creates accumulation & upward moves
-More aggresive selling than buing (supply) creates distribution & downward moves

We use the wyckoff phases to execute on the right side of this battle 
Wyckoff schematics
Acummulation phase.
where sellers start to los control and buyers start step into the market -> buyers start to cleanly take control and sellers starts to fade away -> spring, final attempt by sellers before buyers completely take control of the market -> markup
     crossing point

It is too comlpicated... not able to watch real time, there is somuch going on and so many mini move to keep track of.
     easy to get mixed up in the schematics
     easy to buy to soon 
     too hard to consistenly execute
     it just simply not necessary if it 

how can we combat the complexity if we cannot do it by tracking the data like this bot..
To simplify 
1. markwdown
downward move before a standard accumulation. 
2. selling climax & accumulation. range: shows a pickup in control of buyers matching the sellers strenght
3. spring
final attempt to go lower. its a push down which takes out the low of the selling climax and accumulation range followed. IN ORDER TO BE CONFIRMED MUST BE FOLLOWED BY A SIGN OF STRENGHT wich is a psuh to the upside and a closure above the selling climax and accumulaiton range. shows a clear takeover of control of buyers

then we see a pullback and our buying point

1. range created 2. range swept 3.range broken (closure is important) 4. pullback to buy 5. profitable markup.

Same for distribution but the other way obviously



Backtesting
process of testing a trading strategy against historical price data to evaluate its viability. It helps build the necessary experience to hold winning trades and avoid emotional hesitation

Essential: 
1. Clear, written trading strategy and plan 
2. A system for journaling and screenshotting trades (notion, folders, trading vault, our own system)
3. backtesting platform. Trading view offers a basi bar replay tool. but forex testes 5 is recommended for its abiliti to habdle long term data an automated statistics (find a platform aimed to trade crypto or build towards it)

The backtesting procress
1. plan define risk parameters 0.5 to 1 % per trade and set strcit, repeatable entry and exit rules 
2 execute. apply strategy to historical charts. 
3.  Review your statistics and screenshots to identify patterns in your winning and losing trades, allowing for strategy refinement (ml we are building i guess)


Pitfall
Backtest overfitting from AFML

AFML insights

Financial markets are non stationary, non staic. constantly changing evolving chaos that progresses on a single imeline of reality. You cannot go back a retest the same market condition with a diferent scenario. THe only thing we have is a single instance oif the past and trying to predict the future with absolute certainity by looking at this single instance is the biggest mistake 

ML == harden sharp ratios but massive dissapointment. flawless strategies perfect of paper does not live in hte brutality of the market. Machine learnign does not solve finance on its own. if used incorrectly it merely amplifies the scale and sped of the mistakes you mamke 

From determinism to probability 

THE Classical illusion
Time: Chronological time bins 
Indicators: Deterministic signals 
Outcomes: Directional guessing
Testing: In-sample overfitting 
Goal: finding a  setup 

THE prado reality
Time: Event based information (volume/dollar bars) 
Indicators: Probabilistic features 
Outcomes: Triple barrier volatilty outcomes 
Testing: Purged cross-validation
Goal: building a production system 

technical analysis provides mathematical features, not deteministic signals 

Prado focuses on probability distributions. What is the Probability Distribution of the outcomes in the pst 10,000 instances where this market strcuture ocurred? 
FUNDAMENTAL MINDSET SHIFT 

Station 1. Time based data hides the true microstructure of the market
     Markets do not move by the ticking of a clock. but by transactions and information flow.  TIme bars obscure signal with noise
     - Restructuring data beyond time stabilized statistical properties. A new bar triggers when specific information flows... 


Station 2 fractional differentiaton solves the stationary versus memory dilemma
     Order flow, liquidity dinamics, microstructure now take the place of lagging moving average. We freed the data from time zones, but is the data itself ready to be fed into the model. Most people explect the system to solve it by feeding the price data directly into the machine learning model However. stationary vs vmemory dillema
     Raw price data contains entire historical memory of the market but it is not statitically stationary meaning its mean and variance change over time. if you feed this data into an algorightm will blow up. ON THE OTHER HAND, if you convert the price into logarithmic return as classical financiers. the data becomes stationary, statisticla tests work but this time you completely that extremely valuable long term memory within the price

     Fractional differentiation 
     Data trasformonly  as much as necessary to become stationary, meaning at the minimum level and the valuale market memory within the price is preserve to the maximum extent. == flawlees data set that is both stable enough for ML models to understand and has not forgotten the historical movement characteristcis of the markets

Labeling
     triple barrier method
     it does not prectic where the price will be in 5 days, instead it calculates, given its volatility environmnet, which will the price hit firts. the profit barries, the stop barrier or the limit?. it teaches the model not only direction, but the dinamics of risk, reward, path, dependency and volatility 

THE Backtest dilusion 

If the strategy works on past data does not mean it will work in the future
test thousnad of combinations, and you will find a high performing strategy by pure chance. This is mathematicallly certain overfitting. 
cycle called sisyphus paradigm 
False positive results are constanly generated 

deflated sharp ratio.. Station 4 purged cross validation surgically removes data leakage. 
     The number of test performed 
     The lenght of the data set
     The correlation between models 

     it shows real advantage or just luck. 
     Sirgically cuts the temporal bond and overlapping data reveals what the model will actually do when faced with unseen reality. prevents data leakage 
     Secondary validation: stress testing true risk via monte carlo alternative historical scenarios rejectic weak walkforward tests

     error in the cross-validation method used in ml the k-fold cross validation method assumes data points are independent of each other however in financial time series. the data are glued together. Yesterdays data affects todays data. 
     In test: 
          Classical kfold -> data leakage occurs between the training set and the test set 


statiosn 5 meta labeling 

low confidene -> veto trade / minimize size 
high confidence -> maximize bet sizing 

Meta labeling delegates the tru direver of returns-how capita is managed and sized to machine learning. Also filters out cofounder bias to prevent a factor mirage 

Staion 6 
Hierarchical risk parity build crisis resistant portfolios... 
Markowitz 
relies on historical correlations that break down exactly when you need then-during crises. Hyper sensitive to noise
HRP MODEL 
machine learning clusters the true structural relationships of assets. buils stable portfolios without relying on flawed historical correlation matrices 

Stop searching for the perfect formation 
shift the queston from " how do i make money?" to how do i avoid producing false results? 
Technical analysis is not the answer. but the raw material to run the the factory 

### AFML — implementación en el bot (puente)

Estudio arriba = teoría (estaciones 1–6). En el repo la ejecución está repartida así:

- **Decisión Engine One (shadow):** `python scripts/ml_v0_engine1.py` el **2026-06-08** (re-run N≈200). Umbrales y freeze → `docs/SYSTEM_BASELINE.md` §8 (2026-05-11) y §7.1 (gates G1–G6).
- **Gap AFML vs código actual (auditoría + backlog):** `docs/audits/afml-wyckoff-gap-2026-06-01.md` — Tier 1 (labels, sample weights), **Tier 1b** (`data_service` / columnas barrier + ventana de label + helper training), Tier 2 (purged CV en v0, deploy meta-label), Tier 3 (dollar bars, fracdiff).
- **Después de edge (si pasa 6/8):** roadmap clasificador + Kelly → `docs/audits/ai-service-audit-2026-03-18.md` Phase 2–4. **No** usar `plans/ai-recalibration.md` (path Claude viejo; reemplazado por meta-label en baseline H6).
- **SMC inducement (otro hilo, post-6/8):** `docs/plans/smc-inducement-pullback-fixes-2026-06-01.md` (features v19, no mezclar con Tier 1b).


### About fvg 

1. Frescura (freshness). Un FVG recién formado, en la pierna de impulso más reciente, tiene más peso que uno que quedó enterrado tres swings atrás.
2. Draw on liquidity. El precio no va al FVG "porque sí" — va buscando liquidez. El FVG es una zona donde reacciona de camino a la liquidez, o se vuelve un imán cuando hay liquidez del otro lado. Si tu FVG sin mitigar está en la dirección donde está el draw, la probabilidad de que lo visite es alta. Si está en contra del draw actual, puede quedarse abierto mucho tiempo.
3. Estructura. Mientras la estructura que creó el FVG siga intacta (mismo sesgo), el FVG mantiene su lógica. Si hubo un CHoCH/BOS que rompió ese sesgo, el FVG no se borra, pero su contexto cambió.
Cuándo sí pierde validez de verdad: cuando el precio lo atraviesa por completo y cierra del otro lado con desplazamiento. Ahí deja de ser zona de reacción en el sentido original y puede convertirse en un IFVG (Inversion FVG) — actúa al revés (un FVG alcista invalidado pasa a funcionar como resistencia). Ojo también con la mitigación parcial: si solo tocó el CE (consequent encroachment, el 50% del gap), mucha gente ya lo considera mitigado. Depende de la regla que uses — toque del borde, toque del CE, o relleno total.
En low time frames la historia es distinta en la práctica, no en la teoría. Los FVGs de 1m/5m/15m son los mismos conceptualmente, pero:

Se forman e invalidan constantemente, el ruido los consume rápido. Un FVG de 5m aislado, sin alineación con HTF, tiene probabilidad baja y "envejece" rápido en sentido funcional.
La forma correcta de usarlos es como refinamiento, no como señal independiente: el HTF FVG (4H/1H) define la zona de interés, y bajás a LTF a buscar el FVG que se forme dentro de esa zona para afinar la entrada con mejor R:R.
Un LTF FVG que apunta en contra de tu sesgo de 4H casi siempre es ruido.

Price action 
Never buy above equal lows, never sell below equal highs.

