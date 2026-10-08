# ----------------------------------------------- RECENT DATA WITHOUT SOLD PRICE -----------------------------------------------



import html
import json
import re

import scrapy


RESULTS_URL = "https://www.sothebys.com/en/results?from=07%2F09%2F2026&to=07%2F10%2F2026&f0=1788719400000-1791311400000&f2=00000164-609b-d1db-a5e6-e9ff01230000&q="


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
    # Fix 1: split on "|" with any spacing (lot 312 had no space before it)
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
        # Fix 2: numbers instead of strings
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


class LotsSpider(scrapy.Spider):

    name = "lots"
    allowed_domains = ["www.sothebys.com", "clientapi.prod.sothelabs.com"]
    start_urls = [RESULTS_URL]
    seen_lots = set()

    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "DOWNLOAD_DELAY": 15,
        # Fix 3: Chinese characters display correctly in Excel
        "FEED_EXPORT_ENCODING": "utf-8-sig",
    }

    def __init__(self, max_auctions=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # run with:  scrapy crawl lots -a max_auctions=2 -O lots.csv
        self.max_auctions = int(max_auctions) if max_auctions else None

    def parse(self, response):
        # STEP 1: collect auctions with their name/date/category from the results page
        cards = response.css("div.Card")
        if self.max_auctions:
            cards = cards[: self.max_auctions]  # e.g. only auctions [0] and [1]
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
            yield scrapy.Request(
                auction_info["auction_url"],
                callback=self.parse_lots,
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




# ----------------------------------------------- RECENT DATA WITH SOLD PRICE -----------------------------------------------
# ------------------TO PERFORM THIS YOU WILL NEED TO GET------------------
# BEARER TOKEN  = PERFORM RIGHT CLICK ON AUCTION LOT URL TO OPEN DEVTOOL -> NETWORK -> DOCS -> YOU CAN SEE THE AUCTION NAME -> GO TO HEADERS -> REQUEST -> YOU CAN GET BEARER TOKEN
# ------------------TO PERFORM THIS YOU WILL NEED TO GET------------------
# COOKIE = GO INTO AUCTION LOT URL AND SELECT LOT SOLD VALUE TO OPEN DEVTOOL -> NETWORK -> FETCH/XHR -> YOU CAN SEE THE GRAPHQL -> GO TO HEADERS -> REQUEST -> YOU CAN GET COOKIE





# import csv
# import html
# import json
# import os
# import re

# import scrapy
# from dotenv import load_dotenv

# load_dotenv()  # reads .env from excercise/artscraper
# TOKEN = os.getenv("SOTHEBYS_TOKEN", "")
# COOKIE = os.getenv("SOTHEBYS_COOKIE", "")
# print("token loaded:", bool(TOKEN), "| cookie loaded:", bool(COOKIE))  # temporary check, delete when it works

# RESULTS_URL = "https://www.sothebys.com/en/results?from=07%2F09%2F2026&to=07%2F10%2F2026&f0=1788719400000-1791311400000&f2=00000164-609b-d1db-a5e6-e9ff01230000&q="
# GRAPHQL_URL = "https://clientapi.prod.sothelabs.com/graphql"
# PAGE_SIZE = 48

# # Same query the site sends, trimmed to the fields we use
# LOT_QUERY = """
# query LotCardsFilterByPaginated($id: String!, $filter: LotCardsConnectionFilter!, $language: TranslationLanguage!, $limit: Int, $offset: Int) {
#   auction(id: $id, language: $language) {
#     lotCards: lotCardsConnection(offset: $offset, limit: $limit, filter: $filter) {
#       hasNextPage
#       totalCount
#       lots {
#         lotId title creatorsDisplayTitle
#         auction { auctionId sapSaleNumber currency slug { name year } }
#         lotNumber { ... on VisibleLotNumber { lotDisplayNumber } }
#         bidState {
#           isClosed reserveMet
#           sold { ... on ResultVisible { isSold premiums { finalPrice: finalPriceV2 { currency amount } } } }
#         }
#         slug { lotSlug }
#         estimateV2 { ... on LowHighEstimateV2 { highEstimate { amount } lowEstimate { amount } } }
#         withdrawnState { state }
#         media(imageSizes: [Large]) { images { renditions { url imageSize } } }
#       }
#     }
#   }
# }
# """

# FIELDS = [
#     "auction_name", "auction_detail", "auction_category", "auction_url",
#     "lot_number", "artist", "title", "year_created", "medium", "dimensions",
#     "estimate_low", "estimate_high", "estimate_currency",
#     "is_closed", "reserve_met", "lot_status", "is_sold", "sold_price", "sold_currency", "withdrawn_state",
#     "lot_id", "auction_id", "sale_number", "lot_url", "image_url",
#     "description", "description_en", "provenance", "literature", "exhibited", "catalogue_note",
# ]
# HEADINGS = {
#     "Description": "description", "Condition report": "condition_report",
#     "Provenance": "provenance", "Literature": "literature",
#     "Exhibited": "exhibited", "Catalogue note": "catalogue_note",
#     "Additional Notices & Disclaimers": "notices",  # boilerplate, only a stop marker
# }
# SIGNATURE = re.compile(r"^(one |all )?(signed|inscribed|dated|stamped|bears|titled|numbered|executed)", re.I)
# MEDIUM = re.compile(
#     r"\b(oil|acrylic|gouache|watercolou?r|ink|chalk|pencil|charcoal|bronze|marble|terracotta|"
#     r"lithograph|etching|aquatint|engraving|screenprint|silkscreen|canvas|paper|panel|linen)\b", re.I)


# # ---------- helpers ----------
# def find(data, test):
#     """Yield every dict in nested JSON that passes `test` (doesn't go inside matches)."""
#     if isinstance(data, dict):
#         if test(data):
#             yield data
#         else:
#             for child in data.values():
#                 yield from find(child, test)
#     elif isinstance(data, list):
#         for child in data:
#             yield from find(child, test)


# def to_int(value):
#     return int(value) if value else None


# def first_image(card):
#     for image in ((card.get("media") or {}).get("images")) or []:
#         for r in image["renditions"]:
#             if r["imageSize"] == "Large":
#                 return r["url"]


# def html_to_text(value):
#     """og:description is HTML; turn it into plain lines."""
#     value = re.sub(r"</p>|<br\s*/?>", "\n", value or "")
#     value = html.unescape(re.sub(r"<[^>]+>", "", value)).replace("\xa0", " ")
#     return re.sub(r"\n{3,}", "\n\n", "\n".join(l.strip() for l in value.split("\n"))).strip()


# def join_fragments(lines):
#     """Inline tags split sentences into several lines; glue them back."""
#     out = []
#     for line in lines:
#         if out and (line[0] in ",.;:)" or line[0].islower()):
#             out[-1] += ("" if line[0] in ",.;:)" else " ") + line
#         else:
#             out.append(line)
#     return "\n".join(out)


# def split_sections(lines):
#     """Group the visible page text under its headings."""
#     sections, key = {}, None
#     for line in lines[lines.index("Lot Details") if "Lot Details" in lines else 0:]:
#         if line == "You May Also Like":
#             break
#         if line in HEADINGS:
#             key = HEADINGS[line]
#             sections[key] = []
#         elif key:
#             sections[key].append(line)
#     return {k: ("\n".join(v) if k == "description" else join_fragments(v)).strip()
#             for k, v in sections.items()}


# def extract_facts(text):
#     """Best-effort size / medium / year from the English description."""
#     facts = {"dimensions": "", "medium": "", "year_created": ""}
#     lines = [l.strip() for l in text.split("\n") if l.strip()]
#     for i, line in enumerate(lines):
#         if re.search(r"\d\s*(?:cm|mm)\b", line):
#             facts["dimensions"] = line
#             j = i - 1
#             while j >= 0 and SIGNATURE.match(lines[j]):
#                 j -= 1
#             if j >= 1 and len(lines[j]) <= 100 and MEDIUM.search(lines[j]):
#                 facts["medium"] = lines[j].rstrip(" ;,")
#             break
#     year = (re.search(r"(?:Executed|Painted|Created|Cast|Conceived|Printed)[^.\n]*?(\d{4})", text)
#             or re.search(r"\bdated[^\n]*?(\d{4})", text))
#     facts["year_created"] = year.group(1) if year else ""
#     return facts


# def parse_card(card):
#     # "Artist | Title" (any spacing); decorative lots have no artist
#     parts = re.split(r"\s*\|\s*", card["title"], maxsplit=1)
#     artist, title = parts if len(parts) > 1 else (card.get("creatorsDisplayTitle") or "", parts[0])

#     auction = card["auction"]
#     slug = auction["slug"]
#     estimate = card.get("estimateV2") or {}
#     bid = card.get("bidState") or {}
#     sold = bid.get("sold") or {}
#     final = (sold.get("premiums") or {}).get("finalPrice") or {}

#     return {
#         "lot_number": card["lotNumber"].get("lotDisplayNumber"),
#         "artist": artist.strip(),
#         "title": title.strip(),
#         "estimate_low": to_int((estimate.get("lowEstimate") or {}).get("amount")),
#         "estimate_high": to_int((estimate.get("highEstimate") or {}).get("amount")),
#         "estimate_currency": auction["currency"],
#         "is_closed": bid.get("isClosed"),
#         "reserve_met": bid.get("reserveMet"),
#         "is_sold": sold.get("isSold"),
#         "sold_price": to_int(final.get("amount")),
#         "sold_currency": final.get("currency"),
#         "withdrawn_state": (card.get("withdrawnState") or {}).get("state"),
#         "lot_url": f"https://www.sothebys.com/en/buy/auction/{slug['year']}/{slug['name']}/{card['slug']['lotSlug']}",
#         "lot_id": card["lotId"],
#         "auction_id": auction["auctionId"],
#         "sale_number": auction["sapSaleNumber"],
#         "image_url": first_image(card),
#     }


# # ---------- spider ----------
# class LotsSpider(scrapy.Spider):
#     name = "lots"
#     allowed_domains = ["www.sothebys.com", "clientapi.prod.sothelabs.com"]
#     start_urls = [RESULTS_URL]
#     custom_settings = {
#         "ROBOTSTXT_OBEY": True,
#         "DOWNLOAD_DELAY": 15,
#         "COOKIES_ENABLED": False,  # we send the Cookie header ourselves
#         "DEFAULT_REQUEST_HEADERS": {"Authorization": TOKEN, "Cookie": COOKIE},  # logged-in session
#     }

#     # run:  scrapy crawl lots -a max_auctions=1 -a out=test.csv
#     #       add -a details=0 for a fast prices-only run (no lot pages)
#     def __init__(self, max_auctions=None, out="lots.csv", details="1", *args, **kwargs):
#         super().__init__(*args, **kwargs)
#         self.max_auctions = int(max_auctions) if max_auctions else None
#         self.details = details == "1"
#         self.seen_lots = set()
#         self.file = open(out, "w", newline="", encoding="utf-8-sig")  # utf-8-sig: Chinese shows in Excel
#         self.writer = csv.DictWriter(self.file, FIELDS, extrasaction="ignore", restval="")
#         self.writer.writeheader()

#     def save(self, item):
#         """Write one row to the CSV immediately, then hand the item back."""
#         self.writer.writerow(item)
#         self.file.flush()
#         return item

#     def closed(self, reason):
#         self.file.close()

#     def parse(self, response):
#         """Results page -> one request per auction."""
#         for card in response.css("div.Card")[: self.max_auctions]:
#             url = card.css("a::attr(href)").get()
#             if url:
#                 info = {
#                     "auction_name": (card.css("div.Card-title::text").get() or "").strip(),
#                     "auction_detail": (card.css("div.Card-details::text").get() or "").strip(),
#                     "auction_category": (card.css("div.Card-category::text").get() or "").strip(),
#                     "auction_url": response.urljoin(url),
#                 }
#                 yield scrapy.Request(info["auction_url"], self.parse_auction, cb_kwargs={"info": info})

#     def parse_auction(self, response, info):
#         """Auction page -> auction id, then fetch ALL lots through GraphQL (logged in, with sold prices)."""
#         raw = response.css("script#__NEXT_DATA__::text").get()
#         card = next(find(json.loads(raw), lambda d: "lotId" in d and "auction" in d), None) if raw else None
#         if not card:
#             return self.logger.warning(f"No lot data on {response.url}")
#         yield self.graphql_request(card["auction"]["auctionId"], 0, info)

#     def graphql_request(self, auction_id, offset, info):
#         payload = {
#             "operationName": "LotCardsFilterByPaginated",
#             "query": LOT_QUERY,
#             "variables": {"id": auction_id, "filter": "ALL", "language": "ENGLISH",
#                           "limit": PAGE_SIZE, "offset": offset},
#         }
#         return scrapy.Request(
#             GRAPHQL_URL, method="POST", body=json.dumps(payload), dont_filter=True,
#             headers={"Content-Type": "application/json", "apollographql-client-name": "Bidclient",
#                      "Origin": "https://www.sothebys.com", "Referer": "https://www.sothebys.com/"},
#             callback=self.parse_graphql_page,
#             errback=lambda f: self.logger.error(f"GraphQL failed: {f.value!r}"),
#             cb_kwargs={"auction_id": auction_id, "offset": offset, "info": info},
#         )

#     def parse_graphql_page(self, response, auction_id, offset, info):
#         data = response.json()
#         if data.get("errors"):
#             self.logger.error(f"GraphQL errors: {data['errors']}")
#         page = ((data.get("data") or {}).get("auction") or {}).get("lotCards") or {}
#         cards = list(find(page, lambda d: "lotId" in d and "lotNumber" in d))
#         self.logger.info(f"GraphQL offset {offset}: {len(cards)} lots of {page.get('totalCount')}")

#         for card in cards:
#             item = {**parse_card(card), **info}
#             if item["lot_id"] in self.seen_lots:
#                 continue
#             self.seen_lots.add(item["lot_id"])
#             if self.details:  # open the lot page for description, provenance, etc.
#                 yield scrapy.Request(item["lot_url"], self.parse_lot_detail,
#                                      errback=self.detail_failed, cb_kwargs={"item": item})
#             else:
#                 yield self.save(item)

#         if cards and page.get("hasNextPage", len(cards) >= PAGE_SIZE):
#             yield self.graphql_request(auction_id, offset + PAGE_SIZE, info)

#     def parse_lot_detail(self, response, item):
#         lines = [t.strip() for t in response.xpath(
#             "//body//text()[not(ancestor::script or ancestor::style)]").getall() if t.strip()]
#         sections = split_sections(lines)

#         og = response.css('meta[property="og:description"]::attr(content)').get()
#         description = html_to_text(og) or sections.get("description", "")
#         description_en = re.split(r"\n-{10,}\n", description, maxsplit=1)[0].strip()

#         # result box on the lot page: ["Lot Sold", "409,600 HKD"]
#         label, amount = (response.css('[data-testid="lotBidAmount"] p::text').getall() + ["", ""])[:2]
#         match = re.match(r"([\d,]+)\s*([A-Z]{3})", amount.strip())
#         if label.strip():
#             item["lot_status"] = label.strip()
#             item["is_sold"] = label.strip() == "Lot Sold"
#         if match and not item.get("sold_price"):
#             item.update(sold_price=int(match[1].replace(",", "")), sold_currency=match[2])

#         item.update(description=description, description_en=description_en,
#                     **{k: sections.get(k, "") for k in ("provenance", "literature", "exhibited", "catalogue_note")},
#                     **extract_facts(description_en))
#         yield self.save(item)

#     def detail_failed(self, failure):
#         self.logger.warning(f"Detail page failed: {failure.request.url}")
#         yield self.save(failure.request.cb_kwargs["item"])  # never lose a lot
