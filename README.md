# unilib

EVM zincirlerinde Uniswap V2/V3/V4 havuzlarını tek bir arayüzle okuma kütüphanesi.

Uniswap protokolü her chain'de aynı; değişen tek şey adresler. Bu kütüphane o farkı
veri haline getirir, böylece aynı kod Robinhood Chain'de, HyperEVM'de, Base'de veya
Arbitrum'da çalışır. PancakeSwap, HyperSwap, Project X gibi fork'lar da Uniswap
mimarisini kullandığı için aynı şekilde desteklenir.

## Tanımlı chain'ler

| Chain | id | V2 | V3 | V4 | Not |
|---|---|---|---|---|---|
| Robinhood Chain | 4663 | okuma | okuma | okuma | Resmi Uniswap V4 deploy'u. Router adresleri doğrulanmadı, swap yok. |
| HyperEVM | 999 | okuma | okuma + swap | — | HyperSwap (bağımsız fork). V3 router klasik arayüz. V2 router bulunamadı. |
| Base | 8453 | okuma + swap | okuma + swap | okuma | Resmi deploy. V3 router `SwapRouter02` arayüzü. USDC de taban varlık. |

Yeni chain eklemek `chains.py`'ye bir `ChainConfig` yazmak demek. Adresleri
**mutlaka zincirde doğrula** - bu üç chain'in hepsinde dokümantasyonda geçen en az bir
adres yanlış çıktı (birinde o adreste hiç kontrat yoktu).

## Kurulum

```bash
pip install -e /home/l/dex/unilib
```

`-e` (editable) kurulum: kütüphaneyi düzenlediğin an değişiklik geçerli olur, tekrar
kurmaya gerek kalmaz. İleride başka bir makineye taşımak istersen kodu bir git
reposuna koyup `pip install git+https://...` demek yeterli, kodda değişiklik gerekmez.

## Hızlı başlangıç

```python
from unilib import CHAINS, load_pool

chain = CHAINS[999]          # HyperEVM
w3 = chain.connect()         # chain_id'yi de doğrular

# V2 / V3 için havuz adresi, V4 için pool id - tipi kendisi anlar
pool = load_pool(chain, "0x314cAdD648F3Cf6E3bBAd925F0685A2129Bce94B", w3=w3)

pool.symbol          # HYPURR   (takip edilen token)
pool.base_symbol     # WHYPE    (taban varlık)
pool.price()         # 1 HYPURR kaç WHYPE
pool.quote_sell(1000)  # 1000 HYPURR satarsam kaç WHYPE alırım
pool.quote_buy(1.5)    # 1.5 WHYPE harcarsam kaç HYPURR alırım
```

Üç versiyon da aynı arayüzü verir; kullanan kodun `if pool_type == "v3"` gibi
dallanmalar yazmasına gerek yoktur.

## Alım / satım

```python
from eth_account import Account
from unilib import CHAINS, load_pool, Swapper

account = Account.from_key(...)      # anahtarı nasıl sakladığın sana kalmış
swapper = Swapper(chain, account, w3=w3)

# Gerçekte ne alacağını sor - ücretsiz, hiçbir şey göndermez
swapper.simulate(pool, chain.wrapped_native, 1.0)

result = swapper.buy(pool, amount_in=1.0, slippage_pct=0.5)
result = swapper.sell(pool, amount_in=1000, slippage_pct=0.5, unwrap=True)

if result:
    print(result.tx_hash)
else:
    print(result.error)
```

`buy()` ve `sell()` ayrı metodlar çünkü gerçekten farklılar:

| | alım (taban → token) | satım (token → taban) |
|---|---|---|
| girdi | native coin, `msg.value` olarak | ERC20 |
| approve | gerekmez | gerekli (bir kez, sınırsız) |
| çıktı | ERC20 | wrapped native — `unwrap=True` ile native |
| V2 fonksiyonu | `swapExactETHForTokens` | `swapExactTokensForETH` |

**Vergili ("slipajlı") tokenler:** `fee_on_transfer=True` geçildiğinde V2'nin
`...SupportingFeeOnTransferTokens` varyantları kullanılır. Bu tokenlerde standart
fonksiyonlar miktarı katı doğruladığı için işlem geri döner. Normal tokenlerde de
zararsızdır, sadece biraz daha fazla gas harcar.

**Neden simülasyon önemli:** minimum çıktı (`amountOutMin`) spot fiyattan
hesaplanırsa fiyat etkisi hesaba katılmaz ve işlem "Too little received" ile geri
döner. Gerçek ölçüm (HyperEVM/HYPURR havuzunda, spot fiyatın ne kadar fazla gösterdiği):

| işlem | spot fazlası |
|---|---|
| 0.1 HYPE | %0.04 |
| 1 HYPE | %0.29 |
| 10 HYPE | %2.78 |

Yani %0.5'lik varsayılan slipajla 10 HYPE'lık bir alım spot fiyata güvenilirse geri
dönerdi. `simulate()` bunu zincire gerçek router kodunu çalıştırtarak ölçer; komisyon,
fiyat etkisi ve varsa transfer vergisi otomatik dahil olur. Cüzdanda para olmasa bile
çalışır (`state_override` ile), böylece "5 HYPE'ım olsa ne alırdım?" sorusu için önce
5 HYPE'a sahip olmak gerekmez.

## Modüller

| Dosya | İçerik |
|---|---|
| `pricing.py` | Saf matematik. Ağ yok, config yok - sayı alır, sayı verir. V2'nin kapalı-form formülü, tick/sqrtPrice → fiyat dönüşümleri, yüzde değişim, slipaj. |
| `chains.py` | `ChainConfig` (adresler + sabitler) ve bilinen chain'ler kaydı. Yeni bir chain eklemek buraya bir satır eklemek demek. |
| `abis.py` | Sadece kullanılan fonksiyonların ABI parçaları. |
| `pools.py` | `Pool` arayüzü ve `V2Pool` / `V3Pool` / `V4Pool`. `load_pool()` tipi otomatik tespit eder. |
| `swaps.py` | `Swapper` - alım, satım, approve, simülasyon. Fiyat okumadan ayrı tutuldu: fiyat takibi cüzdana hiç dokunmamalı. |

## Tasarım kararları

**Neden pool'lar class, matematik düz fonksiyon?**
Pool'lar taşınacak durum (bağlantı, adresler, metadata) ve polimorfizm istiyor -
"fiyat nedir?" sorusu üç versiyonda üç farklı şekilde cevaplanıyor ama soru aynı.
Matematik ise hiçbir şey taşımıyor; düz fonksiyon olarak ağa bağlanmadan test edilebiliyor.

**Neden `token` / `base` var, `token0` / `token1` değil?**
`token0`/`token1` sırası sadece adres sıralamasından geliyor, havuzdan havuza
değişiyor ve anlam taşımıyor. Pratikte her havuz "bir taban varlık + takip edilen bir
token" şeklinde kullanılıyor, kütüphane de bunu bir kez çözüp öyle konuşuyor.
İki taraf da taban varlıksa (ETH/USDC gibi) ya da hiçbiri değilse tahmin yürütmez,
hata verir - o durumda belirsizlik içermeyen `pool.quote(token_in, amount_in)` kullanılır.

**Kesin quote sadece V2'de.**
V2'nin çıktısı kapalı-form bir formülle hesaplanabiliyor, bu yüzden komisyon ve fiyat
etkisi dahil gerçek sonuç veriliyor (`exact_quotes == True`). V3/V4'te likidite her
tick sınırında değişebildiği için böyle bir formül yok; oradaki quote'lar spot fiyat
tahminidir ve büyük işlemlerde gerçekte alacağından fazlasını gösterir. Kesin sonuç
için zincir üzerinde simülasyon (`eth_call`) gerekir - henüz eklenmedi.

**V4'te pool_id neden tersine çevrilebiliyor?**
Çevrilmiyor - pool id, PoolKey'in hash'i. Ama PoolManager havuz kurulurken bir
`Initialize` event'i yayınlıyor ve bu event pool id ile indekslenmiş halde fee,
tickSpacing, hooks ve iki token'ı taşıyor. `fetch_v4_pool_key()` bu log'u okuyor.

**Hook uyarısı.**
V4'te `hooks` adresi sıfır değilse, havuzun ekonomisi standart olmayabilir - `fee: 0`
görünen bir havuz hook aracılığıyla kendi kesintisini alıyor olabilir. `pool.has_hooks`
bunu işaret eder; böyle havuzlarda quote'lara ekstra şüpheyle yaklaşmak gerekir.

## Test

```bash
python tests/test_pricing.py
```

Saf matematik testleri, ağ gerektirmez. Bu katmanın test edilmesi önemli çünkü buradaki
bir yön/işaret hatası hata vermez, sadece makul görünen yanlış bir sayı üretir.

## Bilinen kısıtlar

**V4 swap yok.** Fiyat okuma çalışıyor ama alım/satım desteklenmiyor. V4'te havuzlar
tek bir PoolManager içinde yaşıyor ve doğrudan swap `unlock`/`unlockCallback`
mimarisi gerektiriyor - sıradan bir cüzdandan (EOA) bunu yapmanın yolu Universal
Router üzerinden geçmek, o da ayrı bir encode katmanı demek.

**Satım simülasyonu allowance ister.** Router `transferFrom` yaptığı için, izin
verilmemişse simülasyon da geri döner. Bu yüzden `sell()` önce approve eder, sonra
simüle eder.

**Router adresleri doğrulanmalı.** Kütüphaneye eklenen adresler bytecode'undan
kontrol edildi. Yeni bir chain eklerken aynısını yap - dokümantasyonda yazan bir
adresin o chain'de hiç kontrat olmadığı görüldü. Adres bulunamıyorsa `None` bırak;
kütüphane net hata verir, yanlış adrese işlem göndermez.

**İki farklı V3 router arayüzü var.** Klasik `ISwapRouter`'da `deadline` params
struct'ının içinde, `SwapRouter02`'de ise dışarı alınmış ve `multicall(deadline, data)`
ile uygulanıyor. Hangisinin deploy edildiği chain'e göre değişiyor (HyperSwap klasik,
Base router02). Kütüphane bunu bytecode'dan otomatik tespit ediyor; `ChainConfig`'de
`v3_router_variant` ile açıkça da belirtilebilir.

**Havuzun iki tarafı da taban varlıksa** (WETH/USDC gibi) kütüphane hangisinin
"takip edilen token" olduğunu tahmin etmez, hata verir. O havuzlarda belirsizlik
içermeyen `pool.quote(token_in, amount_in)` kullanılır.

**Public RPC limitleri.** HyperEVM'in public RPC'si dakikada 100 istekle sınırlı.
Sıkı döngülerde `w3`'ü tekrar tekrar oluşturmak yerine bir kez oluşturup paylaş
(`load_pool(..., w3=w3)`), yoksa her çağrı `chain_id` doğrulaması için ekstra istek harcar.

**V4 pool key araması baştan tarar.** `fetch_v4_pool_key()` varsayılan olarak
`fromBlock=0` kullanır. Chain büyüdükçe bazı RPC'ler bu aralığı reddedebilir;
o durumda deploy bloğunu `from_block` olarak geçmek gerekir.

## Henüz yok

- V4 swap (Universal Router encode katmanı)
- Multicall ile tek istekte toplu fiyat okuma
- Vergili token oranını otomatik ölçme (`detect_fee_on_transfer`)
- Terminal/izleme yardımcıları (ayrı bir pakete gidecek)
