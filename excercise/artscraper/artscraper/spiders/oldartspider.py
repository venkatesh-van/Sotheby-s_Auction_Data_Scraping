"""
Run:
  scrapy crawl lots_2015 -o lots_2015.csv -s JOBDIR=crawl-state-2015
  scrapy crawl lots_2015 -a start_auction=2 -a max_auctions=2 -o part.csv
  scrapy crawl lots_2015 -a url="https://www.sothebys.com/en/results?..." -o other.csv
"""

import html
import json
import re

import scrapy


# =====================================================================
# 1. SETTINGS  (the things you may want to change)
# =====================================================================

RESULTS_URL = (
    "https://www.sothebys.com/en/results?from=02%2F08%2F2015&to=01%2F09%2F2015"
    "&f0=1438453800000-1441045800000&f2=00000164-609b-d1db-a5e6-e9ff01230000&q="
)

GRAPHQL_URL = "https://clientapi.prod.sothelabs.com/graphql"  # the site's own lot-list API
PAGE_SIZE = 48  # lots per GraphQL page

# Same query the website sends, trimmed to the fields this spider uses.
LOT_QUERY = """
query LotCardsFilterByPaginated($id: String!, $filter: LotCardsConnectionFilter!, $language: TranslationLanguage!, $limit: Int, $offset: Int) {
  auction(id: $id, language: $language) {
    __typename
    id
    lotCards: lotCardsConnection(offset: $offset, limit: $limit, filter: $filter) {
      __typename
      lots {
        __typename
        id
        lotId
        title
        creatorsDisplayTitle
        auction {
          __typename
          auctionId
          sapSaleNumber
          currency
          slug { __typename name year }
        }
        lotNumber {
          __typename
          ... on VisibleLotNumber { __typename lotDisplayNumber }
        }
        bidState {
          __typename
          id
          isClosed
          reserveMet
          numberOfBids
          sold {
            __typename
            ... on ResultHidden { __typename lotId }
            ... on ResultVisible {
              __typename
              isSold
              premiums {
                __typename
                finalPrice: finalPriceV2 { __typename currency amount }
              }
            }
          }
        }
        slug { __typename lotSlug }
        estimateV2 {
          __typename
          ... on LowHighEstimateV2 {
            __typename
            highEstimate { __typename amount }
            lowEstimate { __typename amount }
          }
          ... on EstimateUponRequest { __typename estimateUponRequest }
        }
        withdrawnState { __typename state }
        media(imageSizes: [ExtraSmall, Small, Medium, Large, ExtraLarge]) {
          __typename
          images {
            __typename
            title
            renditions { __typename width height url imageSize }
          }
        }
      }
      hasNextPage
      totalCount
    }
  }
}
"""

# Column order of the final CSV (a missing value simply stays blank).
CSV_COLUMNS = [
    "lot_number", "artist", "title", "estimate_low", "estimate_high", "estimate_currency",
    "sold_price", "sold_currency", "is_sold", "is_closed", "reserve_met", "withdrawn_state",
    "description", "description_en", "medium", "dimensions", "year_created",
    "provenance", "literature", "exhibited", "catalogue_note",
    "auction_name", "auction_detail", "auction_category", "sale_number",
    "auction_id", "lot_id", "lot_url", "auction_url", "image_url",
]


# =====================================================================
# 2. SMALL HELPERS  (numbers, JSON search)
# =====================================================================

def to_int(value):
    """'150000' -> 150000 and None -> None.
    Time: O(len(value))."""
    return int(value) if value else None


def deref(value, cache):
    """The page JSON sometimes stores {"__ref": "Key"} instead of the object itself.
    Return the real dict either way.
    Time: O(1) (one dict lookup)."""
    if isinstance(value, dict) and "__ref" in value:
        return cache.get(value["__ref"], {})
    return value or {}


def find_lot_cache(data):
    """Find the dict that holds the "LotCard:..." entries inside __NEXT_DATA__.
    Time: O(N). Every node is visited at most once; each dict also scans its keys (O(k))."""
    if isinstance(data, dict):
        if any(key.startswith("LotCard:") for key in data):
            return data
        children = data.values()
    elif isinstance(data, list):
        children = data
    else:
        return None

    for child in children:
        found = find_lot_cache(child)
        if found:
            return found
    return None


def find_cards(data, found=None):
    """Collect every lot-card dict anywhere in a GraphQL response.
    Time: O(N). One pass over the response."""
    found = [] if found is None else found
    if isinstance(data, dict):
        if "lotId" in data and "lotNumber" in data:
            found.append(data)  # a lot card: no need to look inside it
        else:
            for child in data.values():
                find_cards(child, found)
    elif isinstance(data, list):
        for child in data:
            find_cards(child, found)
    return found


def first_image(card):
    """URL of the first 'Large' picture of a lot, or None.
    Time: O(k + r), k = keys of the card, r = picture sizes (about 5)."""
    for key, value in card.items():
        if key == "media" or key.startswith("media("):  # first page uses "media(args)"
            images = (value or {}).get("images") or []
            for rendition in (images[0]["renditions"] if images else []):
                if rendition["imageSize"] == "Large":
                    return rendition["url"]
    return None


# =====================================================================
# 3. TEXT HELPERS  (turn page text into clean columns)
# =====================================================================

# Compiled once, reused for every lot: compiling inside a function would repeat the work.
TAGS = re.compile(r"<[^>]+>")
LINE_BREAKS = re.compile(r"</p>|<br\s*/?>")
DASH_LINE = re.compile(r"\n-{10,}\n")  # separates English from Chinese in a description
DIMENSIONS = re.compile(r"\d\s*(?:cm|mm)\b")
SIGNATURE_LINE = re.compile(
    r"^(one |all )?(signed|inscribed|dated|stamped|bears|titled|numbered|executed)", re.I)
MEDIUM_WORDS = re.compile(
    r"\b(oil|acrylic|gouache|watercolou?r|ink|chalk|pencil|charcoal|bronze|marble|terracotta|"
    r"lithograph|etching|aquatint|engraving|screenprint|silkscreen|canvas|paper|panel|linen)\b", re.I)
YEAR_AFTER_VERB = re.compile(r"(?:Executed|Painted|Created|Cast|Conceived|Printed)[^.\n]*?(\d{4})")
YEAR_AFTER_DATED = re.compile(r"\bdated[^\n]*?(\d{4})")
ESTIMATE = re.compile(r"Estimate[:\s]*([\d,]+)\s*[-\u2013]\s*([\d,]+)\s*([A-Z]{3})?")
SOLD = re.compile(r"(?:Lot Sold|Sold for|Price Reali[sz]ed)[:\s]*([\d,]+)\s*([A-Z]{3})?", re.I)

# Headings found on lot pages -> the column they fill (None = ignore that section).
HEADINGS = {
    "description": "description",
    "provenance": "provenance",
    "literature": "literature",
    "exhibited": "exhibited",
    "catalogue note": "catalogue_note",
    "essay": "catalogue_note",
    "condition report": None,
    "additional notices & disclaimers": None,  # export boilerplate
}


def html_to_text(value):
    value = LINE_BREAKS.sub("\n", value or "")
    value = html.unescape(TAGS.sub("", value)).replace("\xa0", " ")
    lines = [line.strip() for line in value.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def join_fragments(lines):
    groups = []
    for line in lines:
        if groups and line[0] in ",.;:)":
            groups[-1].append(line)  # punctuation sticks to the previous word
        elif groups and line[0].islower():
            groups[-1].append(" " + line)  # lowercase word continues the sentence
        else:
            groups.append([line])
    return "\n".join("".join(group) for group in groups)


def split_sections(lines):
    lowered = [line.lower() for line in lines]
    start = lowered.index("lot details") if "lot details" in lowered else 0

    sections, current = {}, None
    for line, key in zip(lines[start:], lowered[start:]):
        if key == "you may also like":
            break  # everything after this is other lots
        if key in HEADINGS:
            current = HEADINGS[key]
            if current:
                sections.setdefault(current, [])
        elif current:
            sections[current].append(line)

    return {
        name: "\n".join(text) if name == "description" else join_fragments(text)
        for name, text in sections.items()
    }


def extract_facts(description_en):
    facts = {"dimensions": "", "medium": "", "year_created": ""}
    lines = [line.strip() for line in description_en.split("\n") if line.strip()]

    for i, line in enumerate(lines):
        if DIMENSIONS.search(line):
            facts["dimensions"] = line
            j = i - 1  # the medium is usually the line just above the size...
            while j >= 0 and SIGNATURE_LINE.match(lines[j]):
                j -= 1  # ...unless a "signed / dated" line sits in between
            if j >= 1 and len(lines[j]) <= 100 and MEDIUM_WORDS.search(lines[j]):
                facts["medium"] = lines[j].rstrip(" ;,")
            break

    year = YEAR_AFTER_VERB.search(description_en) or YEAR_AFTER_DATED.search(description_en)
    if year:
        facts["year_created"] = year.group(1)
    return facts


def detail_fields(description, lines):

    sections = split_sections(lines)
    description = description or sections.get("description", "")
    description_en = DASH_LINE.split(description, maxsplit=1)[0].strip()

    fields = {
        "description": description,
        "description_en": description_en,
        "provenance": sections.get("provenance", ""),
        "literature": sections.get("literature", ""),
        "exhibited": sections.get("exhibited", ""),
        "catalogue_note": sections.get("catalogue_note", ""),
    }
    fields.update(extract_facts(description_en))
    return fields


def visible_lines(response):
    nodes = response.xpath("//body//text()[not(ancestor::script) and not(ancestor::style)]")
    return [text.strip() for text in nodes.getall() if text.strip()]


# =====================================================================
# 4. NEW-SITE LOT CARD  (/en/buy/auction/...)
# =====================================================================

def parse_card(card, cache):
    # "Artist | Title"; some lots (furniture, silver...) have no "|": the whole text is the title
    parts = re.split(r"\s*\|\s*", card["title"], maxsplit=1)
    if len(parts) > 1:
        artist, title = parts
    else:
        artist, title = card.get("creatorsDisplayTitle") or "", parts[0]

    auction = card["auction"]
    slug = auction["slug"]
    estimate = card.get("estimateV2") or {}

    # On the first page these are references into `cache`; on GraphQL pages they are inline.
    bid_state = deref(card.get("bidState"), cache)
    sold = deref(bid_state.get("sold"), cache)
    premiums = deref(sold.get("premiums"), cache)
    final = deref(premiums.get("finalPrice") or premiums.get("finalPriceV2"), cache)
    withdrawn = deref(card.get("withdrawnState"), cache)

    return {
        "lot_number": card["lotNumber"].get("lotDisplayNumber"),
        "artist": (artist or "").strip(),
        "title": title.strip(),
        "estimate_low": to_int((estimate.get("lowEstimate") or {}).get("amount")),
        "estimate_high": to_int((estimate.get("highEstimate") or {}).get("amount")),
        "estimate_currency": auction["currency"],
        "is_closed": bid_state.get("isClosed"),
        "reserve_met": bid_state.get("reserveMet"),
        "is_sold": sold.get("isSold"),  # only filled when the site shows results without login
        "sold_price": to_int(final.get("amount")),  # includes the buyer's premium
        "sold_currency": final.get("currency"),
        "withdrawn_state": withdrawn.get("state"),
        "lot_url": f"https://www.sothebys.com/en/buy/auction/{slug['year']}/{slug['name']}/{card['slug']['lotSlug']}",
        "lot_id": card["lotId"],
        "auction_id": auction["auctionId"],
        "sale_number": auction["sapSaleNumber"],
        "image_url": first_image(card),
    }


# =====================================================================
# 5. OLDER LOT PAGES  (/en/auctions/<year>/...html)
# =====================================================================

LEGACY_LOT_NUMBER = re.compile(r"lot\.(\d+)\.html")
LEGACY_SALE_NUMBER = re.compile(r"-([a-z]\d{4,5})/", re.I)  # e.g. ...-n09375/lot.1.html


def legacy_prices(page_text):
    prices = {"estimate_low": None, "estimate_high": None, "estimate_currency": "",
              "sold_price": None, "sold_currency": ""}

    estimate = ESTIMATE.search(page_text)
    if estimate:
        prices["estimate_low"] = int(estimate.group(1).replace(",", ""))
        prices["estimate_high"] = int(estimate.group(2).replace(",", ""))
        prices["estimate_currency"] = estimate.group(3) or ""

    sold = SOLD.search(page_text)
    if sold:
        prices["sold_price"] = int(sold.group(1).replace(",", ""))
        prices["sold_currency"] = sold.group(2) or ""
    return prices


def legacy_item(url, artist, page_title, description, lines):
    lot_number = (LEGACY_LOT_NUMBER.search(url) or [None, None])[1]
    sale = LEGACY_SALE_NUMBER.search(url + "/")
    sale_number = sale.group(1).upper() if sale else ""

    # Description list is: artist, title, signature, medium, size ...
    desc_lines = description.split("\n")
    title = desc_lines[1] if len(desc_lines) > 1 and desc_lines[0] == artist else page_title

    page_text = " ".join(lines)
    item = {
        "lot_number": lot_number,
        "artist": artist,
        "title": title,
        "is_closed": "bidding is closed" in page_text.lower(),
        "lot_url": url,
        "lot_id": f"{sale_number}-lot{lot_number}",
        "auction_id": sale_number,
        "sale_number": sale_number,
    }
    item.update(detail_fields(description, lines))
    item.update(legacy_prices(page_text))
    return item


# =====================================================================
# 6. SAVE EVERY LOT AS SOON AS IT IS SCRAPED
# =====================================================================

class LiveSavePipeline:

    def open_spider(self, spider):
        # "a" = append, so a resumed run (JOBDIR) keeps the earlier rows
        self.file = open(spider.settings.get("LIVE_FILE", "lots_live.jsonl"), "a", encoding="utf-8")
        self.count = 0

    def process_item(self, item, spider):
        self.file.write(json.dumps(dict(item), ensure_ascii=False) + "\n")
        self.file.flush()
        self.count += 1
        spider.logger.info(
            f"[{self.count}] lot {item.get('lot_number')} | {(item.get('artist') or '')[:25]} | "
            f"{(item.get('title') or '')[:35]} | est {item.get('estimate_low')}-"
            f"{item.get('estimate_high')} {item.get('estimate_currency')} | sold {item.get('sold_price')}"
        )
        return item

    def close_spider(self, spider):
        self.file.close()


# =====================================================================
# 7. THE SPIDER  (the order below is the order things happen)
# =====================================================================

class Lots2015Spider(scrapy.Spider):
    name = "lots_2015"
    allowed_domains = ["www.sothebys.com", "clientapi.prod.sothelabs.com"]

    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "DOWNLOAD_DELAY": 5,  # seconds between requests; raise it to be gentler
        "FEED_EXPORT_ENCODING": "utf-8-sig",  # Chinese text opens correctly in Excel
        "FEED_EXPORT_FIELDS": CSV_COLUMNS,
        "ITEM_PIPELINES": {f"{__name__}.LiveSavePipeline": 100},
        "LIVE_FILE": "lots_2015_live.jsonl",
        "LOG_LEVEL": "INFO",  # use -s LOG_LEVEL=DEBUG for full dumps
    }

    def __init__(self, start_auction=0, max_auctions=None, url=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.start_urls = [url or RESULTS_URL]
        self.first = int(start_auction)
        self.last = self.first + int(max_auctions) if max_auctions else None
        self.seen = set()  # lots already requested; a set checks membership in O(1)

    # ---- step 1: the results page lists the auctions ----

    def parse(self, response):
        for card in response.css("div.Card")[self.first : self.last]:
            url = card.css("a::attr(href)").get()
            if not url:
                continue

            auction_info = {
                "auction_name": (card.css("div.Card-title::text").get() or "").strip(),
                "auction_detail": (card.css("div.Card-details::text").get() or "").strip(),
                "auction_category": (card.css("div.Card-category::text").get() or "").strip(),
                "auction_url": response.urljoin(url),
            }
            self.logger.info(f"Auction: {auction_info['auction_name']} -> {auction_info['auction_url']}")

            # new-site auctions live under /buy/auction/; older ones end in .html
            is_new = "/buy/auction/" in auction_info["auction_url"]
            yield scrapy.Request(
                auction_info["auction_url"],
                callback=self.parse_lots if is_new else self.parse_legacy_auction,
                cb_kwargs={"auction_info": auction_info},
            )

    # ---- step 2a: new-site auction page (first 48 lots are inside the page JSON) ----

    def parse_lots(self, response, auction_info):
        raw = response.css("script#__NEXT_DATA__::text").get()
        cache = find_lot_cache(json.loads(raw)) if raw else None
        if not cache:
            self.logger.warning(f"No lot data found on {response.url}")
            return

        cards = [value for key, value in cache.items() if key.startswith("LotCard:")]
        self.logger.info(f"Found {len(cards)} lots on {response.url}")
        yield from self.request_lot_pages(cards, cache, auction_info)

        if len(cards) >= PAGE_SIZE:  # a full page means there may be more lots
            auction_id = cards[0]["auction"]["auctionId"]
            yield self.graphql_request(auction_id, PAGE_SIZE, auction_info)

    def request_lot_pages(self, cards, cache, auction_info):
        """Turn cards into rows and ask for each lot's own page (for the description).
        Time: O(C). One set lookup per card."""
        for card in cards:
            item = parse_card(card, cache)
            if item["lot_id"] in self.seen:
                continue
            self.seen.add(item["lot_id"])
            item.update(auction_info)
            yield scrapy.Request(
                item["lot_url"],
                callback=self.parse_lot_detail,
                errback=self.detail_failed,
                cb_kwargs={"item": item},
            )

    # ---- step 2b: the remaining lots, 48 at a time, from the GraphQL API ----

    def graphql_request(self, auction_id, offset, auction_info):
        payload = {
            "operationName": "LotCardsFilterByPaginated",
            "query": LOT_QUERY,
            "variables": {"id": auction_id, "filter": "ALL", "language": "ENGLISH",
                          "limit": PAGE_SIZE, "offset": offset},
        }
        return scrapy.Request(
            GRAPHQL_URL,
            method="POST",
            body=json.dumps(payload),
            headers={
                "Content-Type": "application/json",
                "Accept": "*/*",
                "apollographql-client-name": "Bidclient",
                "Origin": "https://www.sothebys.com",
                "Referer": "https://www.sothebys.com/",
            },
            callback=self.parse_graphql_page,
            errback=self.graphql_failed,
            cb_kwargs={"auction_id": auction_id, "offset": offset, "auction_info": auction_info},
            dont_filter=True,  # same URL every time, so skip Scrapy's duplicate filter
        )

    def parse_graphql_page(self, response, auction_id, offset, auction_info):
        data = response.json()
        if data.get("errors"):
            self.logger.error(f"GraphQL errors: {data['errors']}")

        connection = ((data.get("data") or {}).get("auction") or {}).get("lotCards") or {}
        cards = find_cards(connection)
        self.logger.info(f"GraphQL offset {offset}: {len(cards)} lots (total {connection.get('totalCount')})")
        yield from self.request_lot_pages(cards, {}, auction_info)

        has_next = connection.get("hasNextPage")
        if has_next is None:
            has_next = len(cards) >= PAGE_SIZE
        if has_next and cards:
            yield self.graphql_request(auction_id, offset + PAGE_SIZE, auction_info)

    def graphql_failed(self, failure):
        self.logger.error(f"GraphQL request failed: {failure.value!r}")

    # ---- step 3: a new-site lot page adds the description columns ----

    def parse_lot_detail(self, response, item):
        
        og = response.css('meta[property="og:description"]::attr(content)').get()
        item.update(detail_fields(html_to_text(og), visible_lines(response)))
        yield item

    def detail_failed(self, failure):
        
        self.logger.warning(f"Detail page failed: {failure.request.url}")
        yield failure.request.cb_kwargs["item"]

    # ---- older pages: auction page -> lot pages ----

    def parse_legacy_auction(self, response, auction_info):
        hrefs = response.css('a[href*="/ecatalogue/"]::attr(href)').getall()
        urls = dict.fromkeys(response.urljoin(href) for href in hrefs)
        self.logger.info(f"Found {len(urls)} lots on {response.url}")

        for url in urls:
            if url not in self.seen:
                self.seen.add(url)
                yield scrapy.Request(
                    url,
                    callback=self.parse_legacy_lot,
                    errback=self.legacy_failed,
                    cb_kwargs={"auction_info": auction_info},
                )

        # best effort: follow a "Next" link if the lot list has more pages
        next_page = response.xpath('//a[normalize-space()="Next"]/@href').get()
        if next_page and next_page != "#" and not next_page.startswith("javascript"):
            yield response.follow(next_page, callback=self.parse_legacy_auction,
                                  cb_kwargs={"auction_info": auction_info})

    def parse_legacy_lot(self, response, auction_info):
        items = response.xpath(
            '//*[self::h2 or self::h3][normalize-space()="Description"]/following::ul[1]/li')
        texts = ("".join(li.xpath(".//text()").getall()) for li in items)
        description = "\n".join(t for t in (" ".join(text.split()) for text in texts) if t)

        item = legacy_item(
            url=response.url,
            artist=" ".join(response.css("h1 ::text").getall()).strip(),
            page_title=(response.css("title::text").get() or "").strip(),
            description=description,
            lines=visible_lines(response),
        )
        item.update(auction_info)
        yield item

    def legacy_failed(self, failure):
        """Time: O(1)."""
        self.logger.warning(f"Lot page failed: {failure.request.url}")
