import html
import json
import re

import scrapy


RESULTS_URL = "https://www.sothebys.com/en/results?from=02%2F08%2F2015&to=01%2F09%2F2015&f0=1438453800000-1441045800000&f2=00000164-609b-d1db-a5e6-e9ff01230000&q="


# ---- PAGINATION SETTINGS (taken from the site's own "graphql" request) ----
GRAPHQL_URL = "https://clientapi.prod.sothelabs.com/graphql"
PAGE_SIZE = 48

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


def find_lot_cache(data):
    """Search the JSON for the dict that holds the 'LotCard:...' entries."""
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


def first_image(card):
    for key, value in card.items():
        if key == "media" or key.startswith("media("):
            images = (value or {}).get("images") or []
            if images:
                for rendition in images[0]["renditions"]:
                    if rendition["imageSize"] == "Large":
                        return rendition["url"]
    return None


SECTION_HEADINGS = {
    "description": "Description",
    "condition_report": "Condition report",
    "provenance": "Provenance",
    "literature": "Literature",
    "exhibited": "Exhibited",
    "catalogue_note": "Catalogue note",
    "notices": "Additional Notices & Disclaimers",  # export boilerplate, dropped
}


def html_to_text(value):
    """og:description holds HTML like <p>..</p>; turn it into plain lines."""
    value = re.sub(r"</p>|<br\s*/?>", "\n", value or "")
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value).replace("\xa0", " ")
    lines = [line.strip() for line in value.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def join_fragments(lines):
    """Italic/inline tags split one sentence into several lines; glue them back."""
    out = []
    for line in lines:
        if out and (line[0] in ",.;:)" or line[0].islower()):
            out[-1] += ("" if line[0] in ",.;:)" else " ") + line
        else:
            out.append(line)
    return "\n".join(out)


def split_sections(lines):
    """Group the visible text of a lot page under its headings."""
    heading_names = {v: k for k, v in SECTION_HEADINGS.items()}
    sections, current = {}, None
    try:
        start = lines.index("Lot Details")
    except ValueError:
        start = 0
    for line in lines[start:]:
        if line == "You May Also Like":
            break
        if line in heading_names:
            current = heading_names[line]
            sections[current] = []
        elif current:
            sections[current].append(line)
    return {k: ("\n".join(v) if k == "description" else join_fragments(v)).strip()
            for k, v in sections.items()}


SIGNATURE_LINE = re.compile(r"^(one |all )?(signed|inscribed|dated|stamped|bears|titled|numbered|executed)", re.I)
MEDIUM_WORDS = re.compile(
    r"\b(oil|acrylic|gouache|watercolou?r|ink|chalk|pencil|charcoal|bronze|marble|terracotta|"
    r"lithograph|etching|aquatint|engraving|screenprint|silkscreen|canvas|paper|panel|linen)\b", re.I)


def extract_facts(description_en):
    """Best-effort size / medium / year from the English description (heuristic)."""
    facts = {"dimensions": "", "medium": "", "year_created": ""}
    lines = [l.strip() for l in description_en.split("\n") if l.strip()]
    for i, line in enumerate(lines):
        if re.search(r"\d\s*(?:cm|mm)\b", line):
            facts["dimensions"] = line
            j = i - 1
            while j >= 0 and SIGNATURE_LINE.match(lines[j]):
                j -= 1
            if j >= 1 and len(lines[j]) <= 100 and MEDIUM_WORDS.search(lines[j]):
                facts["medium"] = lines[j].rstrip(" ;,")
            break
    year = re.search(r"(?:Executed|Painted|Created|Cast|Conceived|Printed)[^.\n]*?(\d{4})", description_en)
    if not year:
        year = re.search(r"\bdated[^\n]*?(\d{4})", description_en)
    if year:
        facts["year_created"] = year.group(1)
    return facts


def find_cards(data, out=None):
    """Find every lot-card dict anywhere in a GraphQL response."""
    out = [] if out is None else out
    if isinstance(data, dict):
        if "lotId" in data and "lotNumber" in data:
            out.append(data)
        else:
            for child in data.values():
                find_cards(child, out)
    elif isinstance(data, list):
        for child in data:
            find_cards(child, out)
    return out


def to_int(value):
    return int(value) if value else None


def deref(value, cache):
    """Apollo cache entries may be {"__ref": key}; return the real dict."""
    if isinstance(value, dict) and "__ref" in value:
        return cache.get(value["__ref"], {})
    return value or {}


def parse_card(card, cache):
    # split on "|" with any spacing (some titles have no space before it)
    parts = re.split(r"\s*\|\s*", card["title"], maxsplit=1)
    if len(parts) > 1:
        artist, title = parts
    else:  # decorative-art lots: the whole text is the object name, not "Artist | Title"
        artist, title = card.get("creatorsDisplayTitle") or "", parts[0]

    auction = card["auction"]
    slug = auction["slug"]
    estimate = card.get("estimateV2") or {}
    # first page: bidState is a reference into the cache; GraphQL pages: it is inline
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
        "sold_price": to_int(final.get("amount")),
        "sold_currency": final.get("currency"),
        "withdrawn_state": withdrawn.get("state"),
        "lot_url": f"https://www.sothebys.com/en/buy/auction/{slug['year']}/{slug['name']}/{card['slug']['lotSlug']}",
        "lot_id": card["lotId"],
        "auction_id": auction["auctionId"],
        "sale_number": auction["sapSaleNumber"],
        "image_url": first_image(card),
    }


# ---------------- older Sotheby's page format (e.g. 2015 auctions) ----------------

LEGACY_HEADINGS = {
    "description": "description",
    "provenance": "provenance",
    "literature": "literature",
    "exhibited": "exhibited",
    "catalogue_note": "catalogue note",
    "essay": "essay",
    "condition_report": "condition report",
}


def legacy_sections(lines):
    """Group page text under headings (case-insensitive)."""
    names = {v: k for k, v in LEGACY_HEADINGS.items()}
    sections, current = {}, None
    for line in lines:
        key = line.strip().lower()
        if key in names:
            current = names[key]
            sections[current] = []
        elif current:
            sections[current].append(line)
    return {k: join_fragments(v) for k, v in sections.items()}


def legacy_prices(page_text):
    """Estimate / sold price if the page shows them (blank otherwise)."""
    out = {"estimate_low": None, "estimate_high": None, "estimate_currency": "",
           "sold_price": None, "sold_currency": ""}
    est = re.search(r"Estimate[:\s]*([\d,]+)\s*[-\u2013]\s*([\d,]+)\s*([A-Z]{3})?", page_text)
    if est:
        out["estimate_low"] = int(est.group(1).replace(",", ""))
        out["estimate_high"] = int(est.group(2).replace(",", ""))
        out["estimate_currency"] = est.group(3) or ""
    sold = re.search(r"(?:Lot Sold|Sold for|Price Reali[sz]ed)[:\s]*([\d,]+)\s*([A-Z]{3})?", page_text, re.I)
    if sold:
        out["sold_price"] = int(sold.group(1).replace(",", ""))
        out["sold_currency"] = sold.group(2) or ""
    return out


class LiveSavePipeline:
    """Writes every lot to a JSON-Lines file the moment it is scraped (flushed to disk),
    and prints one short line per lot, so a crash or Ctrl+C never loses data."""

    def open_spider(self, spider):
        path = spider.settings.get("LIVE_FILE", "lots_live.jsonl")
        self.file = open(path, "a", encoding="utf-8")  # append: safe with JOBDIR resume
        self.count = 0

    def process_item(self, item, spider):
        self.file.write(json.dumps(dict(item), ensure_ascii=False) + "\n")
        self.file.flush()
        self.count += 1
        spider.logger.info(
            f"[{self.count}] lot {item.get('lot_number')} | {(item.get('artist') or '')[:25]} | "
            f"{(item.get('title') or '')[:35]} | est {item.get('estimate_low')}-{item.get('estimate_high')} "
            f"{item.get('estimate_currency')} | sold {item.get('sold_price')}"
        )
        return item

    def close_spider(self, spider):
        self.file.close()


class Lots2015Spider(scrapy.Spider):

    name = "lots_2015"
    allowed_domains = ["www.sothebys.com", "clientapi.prod.sothelabs.com"]
    start_urls = [RESULTS_URL]
    seen_lots = set()

    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "DOWNLOAD_DELAY": 5,  # seconds between requests; raise it to be gentler
        # Chinese characters display correctly in Excel
        "FEED_EXPORT_ENCODING": "utf-8-sig",
        # fixed column order, so the CSV never loses a column if the first row lacks a field
        "FEED_EXPORT_FIELDS": [
            "lot_number", "artist", "title", "estimate_low", "estimate_high", "estimate_currency",
            "sold_price", "sold_currency", "is_sold", "is_closed", "reserve_met", "withdrawn_state",
            "description", "description_en", "medium", "dimensions", "year_created",
            "provenance", "literature", "exhibited", "catalogue_note",
            "auction_name", "auction_detail", "auction_category", "sale_number",
            "auction_id", "lot_id", "lot_url", "auction_url", "image_url",
        ],
        "ITEM_PIPELINES": {f"{__name__}.LiveSavePipeline": 100},
        "LIVE_FILE": "lots_2015_live.jsonl",
        "LOG_LEVEL": "INFO",  # one short line per lot; use -s LOG_LEVEL=DEBUG for full dumps
    }

    def __init__(self, start_auction=0, max_auctions=None, url=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if url:
            self.start_urls = [url]
        # scrapy crawl lots_2015 -a start_auction=0 -a max_auctions=2 -o lots_2015.csv
        # any other results link:  -a url="https://www.sothebys.com/en/results?..."
        self.start_auction = int(start_auction)
        self.max_auctions = int(max_auctions) if max_auctions else None

    def parse(self, response):
        # STEP 1: collect auctions with their name/date/category from the results page
        cards = response.css("div.Card")
        end = self.start_auction + self.max_auctions if self.max_auctions else None
        cards = cards[self.start_auction : end]  # e.g. start_auction=2 -> auctions [2], [3], ...
        self.logger.info(f"Found {len(cards)} auction cards")

        for card in cards:
            url = card.css("a::attr(href)").get()
            if not url:
                continue

            auction_info = {
                "auction_name": (card.css("div.Card-title::text").get() or "").strip(),
                "auction_detail": (card.css("div.Card-details::text").get() or "").strip(),
                "auction_category": (card.css("div.Card-category::text").get() or "").strip(),
                "auction_url": response.urljoin(url),
            }
            self.logger.info(f"Auction URL: {auction_info['auction_url']}")

            # STEP 2: request each auction page
            # new site pages live under /buy/auction/; older ones are /en/auctions/<year>/<name>.html
            is_new = "/buy/auction/" in auction_info["auction_url"]
            yield scrapy.Request(
                auction_info["auction_url"],
                callback=self.parse_lots if is_new else self.parse_legacy_auction,
                cb_kwargs={"auction_info": auction_info},
            )

    def parse_lots(self, response, auction_info):
        # STEP 3: read __NEXT_DATA__
        raw = response.css("script#__NEXT_DATA__::text").get()
        if not raw:
            self.logger.warning(f"No __NEXT_DATA__ on {response.url}")
            return

        cache = find_lot_cache(json.loads(raw))
        if not cache:
            self.logger.warning(f"No lot cache found on {response.url}")
            return

        # STEP 4: get lot cards from the first page
        cards = [v for k, v in cache.items() if k.startswith("LotCard:")]
        self.logger.info(f"Found {len(cards)} lots on {response.url}")

        for request in self.emit_cards(cards, cache, auction_info):
            yield request

        # STEP 5: more pages? (the site loads them through GraphQL)
        if GRAPHQL_URL and cards and len(cards) >= PAGE_SIZE:
            auction_id = cards[0]["auction"]["auctionId"]
            yield self.graphql_request(auction_id, PAGE_SIZE, auction_info)

    def emit_cards(self, cards, cache, auction_info):
        for card in cards:
            item = parse_card(card, cache)
            if item["lot_id"] in self.seen_lots:
                continue
            self.seen_lots.add(item["lot_id"])
            item.update(auction_info)
            # open the lot page to get description etc.
            yield scrapy.Request(
                item["lot_url"],
                callback=self.parse_lot_detail,
                errback=self.detail_failed,
                cb_kwargs={"item": item},
            )

    def graphql_request(self, auction_id, offset, auction_info):
        payload = {
            "operationName": "LotCardsFilterByPaginated",
            "query": LOT_QUERY,
            "variables": {
                "id": auction_id,
                "filter": "ALL",
                "language": "ENGLISH",
                "limit": PAGE_SIZE,
                "offset": offset,
            },
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
            errback=self.graphql_failed,
            callback=self.parse_graphql_page,
            cb_kwargs={"auction_id": auction_id, "offset": offset, "auction_info": auction_info},
            dont_filter=True,
        )

    def parse_graphql_page(self, response, auction_id, offset, auction_info):
        data = response.json()
        if data.get("errors"):
            self.logger.error(f"GraphQL errors: {data['errors']}")
        connection = ((data.get("data") or {}).get("auction") or {}).get("lotCards") or {}
        cards = find_cards(connection)
        self.logger.info(
            f"GraphQL offset {offset}: {len(cards)} lots (total {connection.get('totalCount')})"
        )

        for request in self.emit_cards(cards, {}, auction_info):
            yield request

        has_next = connection.get("hasNextPage")
        if has_next is None:
            has_next = len(cards) >= PAGE_SIZE
        if has_next and cards:
            yield self.graphql_request(auction_id, offset + PAGE_SIZE, auction_info)

    def graphql_failed(self, failure):
        self.logger.error(f"GraphQL request failed: {failure.value!r}")

    # ---------- older page format: auction page -> lot pages ----------

    def parse_legacy_auction(self, response, auction_info):
        links, seen = [], set()
        for href in response.css('a[href*="/ecatalogue/"]::attr(href)').getall():
            url = response.urljoin(href)
            if url not in seen:
                seen.add(url)
                links.append(url)
        self.logger.info(f"Found {len(links)} lots on {response.url}")

        for url in links:
            if url in self.seen_lots:
                continue
            self.seen_lots.add(url)
            yield scrapy.Request(
                url,
                callback=self.parse_legacy_lot,
                errback=self.legacy_failed,
                cb_kwargs={"auction_info": auction_info},
            )

        # if the listing has more pages, follow the "Next" link (best effort)
        nxt = response.xpath('//a[normalize-space()="Next"]/@href').get()
        if nxt and nxt != "#" and not nxt.startswith("javascript"):
            yield response.follow(
                nxt, callback=self.parse_legacy_auction, cb_kwargs={"auction_info": auction_info}
            )

    def parse_legacy_lot(self, response, auction_info):
        lines = [
            t.strip()
            for t in response.xpath(
                "//body//text()[not(ancestor::script) and not(ancestor::style)]"
            ).getall()
            if t.strip()
        ]
        page_text = " ".join(lines)
        sections = legacy_sections(lines)

        artist = " ".join(response.css("h1 ::text").getall()).strip()

        # description is a list under the "Description" heading; join each <li> properly
        items = []
        for li in response.xpath(
            '//*[self::h2 or self::h3][normalize-space()="Description"]/following::ul[1]/li'
        ):
            text = re.sub(r"\s+", " ", "".join(li.xpath(".//text()").getall())).strip()
            if text:
                items.append(text)
        description = "\n".join(items) or sections.get("description", "")

        title = ""
        desc_lines = description.split("\n") if description else []
        if len(desc_lines) > 1 and desc_lines[0] == artist:
            title = desc_lines[1]
        title = title or response.css("title::text").get("").strip()

        number = re.search(r"lot\.(\d+)\.html", response.url)
        sale = re.search(r"-([a-z]\d{4,5})(?:/|\.html)", response.url + "/", re.I)
        sale_number = sale.group(1).upper() if sale else ""
        lot_number = number.group(1) if number else None

        item = {
            "lot_number": lot_number,
            "artist": artist,
            "title": title,
            "is_closed": "bidding is closed" in page_text.lower() or None,
            "withdrawn_state": None,
            "is_sold": None,
            "lot_url": response.url,
            "lot_id": f"{sale_number}-lot{lot_number}",
            "auction_id": sale_number,
            "sale_number": sale_number,
            "image_url": response.css(
                'img[src*="brightspot"]::attr(src), img[data-src*="brightspot"]::attr(data-src)'
            ).get(),
            "reserve_met": None,
            "description": description,
            "description_en": description,
            "provenance": sections.get("provenance", ""),
            "literature": sections.get("literature", ""),
            "exhibited": sections.get("exhibited", ""),
            "catalogue_note": sections.get("catalogue_note", "") or sections.get("essay", ""),
        }
        item.update(legacy_prices(page_text))
        item.update(extract_facts(description))
        item.update(auction_info)
        yield item

    def legacy_failed(self, failure):
        self.logger.warning(f"Lot page failed: {failure.request.url}")

    def parse_lot_detail(self, response, item):
        # visible text of the page, line by line (script/style removed)
        lines = [
            t.strip()
            for t in response.xpath(
                "//body//text()[not(ancestor::script) and not(ancestor::style)]"
            ).getall()
            if t.strip()
        ]
        sections = split_sections(lines)

        # the full description (English + Chinese) is also in the og:description meta tag
        og = response.css('meta[property="og:description"]::attr(content)').get()
        description = html_to_text(og) or sections.get("description", "")
        description_en = re.split(r"\n-{10,}\n", description, maxsplit=1)[0].strip()

        item["description"] = description
        item["description_en"] = description_en
        item["provenance"] = sections.get("provenance", "")
        item["literature"] = sections.get("literature", "")
        item["exhibited"] = sections.get("exhibited", "")
        item["catalogue_note"] = sections.get("catalogue_note", "")
        item.update(extract_facts(description_en))
        yield item

    def detail_failed(self, failure):
        # never lose a lot because its detail page failed
        self.logger.warning(f"Detail page failed: {failure.request.url}")
        yield failure.request.cb_kwargs["item"]