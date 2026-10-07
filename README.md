# Sotheby's Auction Data Scraping

A Python web scraper built with **Scrapy** to collect auction and artwork data from the Sotheby's website.

## Project Structure

```text
Sotheby's Scraper/
│
├── artvenv/
│   └── Python virtual environment
│
├── exercise/
│   └── artscraper/
│       │
│       ├── artscraper/
│       │   ├── spiders/
│       │   │   ├── __init__.py
│       │   │   ├── artspider.py
│       │   │   └── oldartspider.py
│       │   │
│       │   ├── __init__.py
│       │   ├── items.py
│       │   ├── middlewares.py
│       │   ├── pipelines.py
│       │   └── settings.py
│       │
│       ├── .env
│       ├── .gitignore
│       ├── august_2015.csv
│       ├── last_30_days_auction.csv
│       ├── recent_data_with_sold.csv
│       └── scrapy.cfg
│
└── README.md
```

## File Explanation

### `artvenv/`

Python virtual environment used for this project.

It contains the packages required to run the scraper.

Activate it with:

```bash
source artvenv/bin/activate
```

---

### `artspider.py`

This is the **main spider**.

It is responsible for:

- Sending requests to Sotheby's
- Getting auction data
- Getting lot data
- Handling GraphQL/JSON responses
- Extracting lot URLs
- Extracting artwork information
- Extracting images
- Cleaning text
- Processing the scraped data

This is the main file used for the current scraper.

---

### `oldartspider.py`

This is the older version of the scraper.

It is kept for reference and comparison.

The current development should mainly use:

```text
artspider.py
```

---

### `items.py`

Defines the structure of the data collected by the scraper.

For example:

```text
auction_name
auction_url
lot_number
lot_title
lot_url
artist
estimate
image_url
description
```

---

### `pipelines.py`

Processes the data after it has been scraped.

It can be used for:

- Cleaning data
- Formatting data
- Removing unwanted values
- Checking duplicates
- Saving processed data

Simple way to understand it:

```text
Spider → Collects data
Pipeline → Processes data
```

---

### `middlewares.py`

Contains Scrapy middleware.

Middleware can be used to:

- Modify requests
- Modify responses
- Add headers
- Handle errors
- Control request behaviour

---

### `settings.py`

Contains the Scrapy project settings.

It controls things such as:

- Pipelines
- Middleware
- User agent
- Request settings
- Download settings
- Logging
- Concurrency

---

### `scrapy.cfg`

Main configuration file for the Scrapy project.

It tells Scrapy where the project settings are located.

---

### `.env`

Contains environment variables and sensitive information such as API keys.

Example:

```text
SOTHEBYS_TOKEN=''
SOTHEBYS_COOKIE=''
```

Do not commit sensitive API keys to GitHub.

---

### CSV Files

The CSV files contain scraped or processed auction data.

Examples:

```text
august_2015.csv
last_30_days_auction.csv
recent_data_with_sold.csv
```

They can be used for:

- Testing
- Data analysis
- Checking scraper results
- Comparing data
- Finding missing data

---

# How the Scraper Works

The basic flow is:

```text
Sotheby's Website
       ↓
     Scrapy
       ↓
  artspider.py
       ↓
GraphQL / JSON Response
       ↓
 Extract Auction & Lot Data
       ↓
     items.py
       ↓
    pipelines.py
       ↓
 Clean / Process Data
       ↓
   CSV / Output
```

# Why GraphQL / JSON?

Sotheby's uses JavaScript to load some of its data.

Because of this, some auction and lot information may not be available directly in the initial HTML.

Instead, the website sends requests to a GraphQL API.

The scraper reads the JSON response and extracts the required information.

For example:

```text
GraphQL API
     ↓
JSON Response
     ↓
Auction
     ↓
Lots
     ↓
Artwork
     ↓
Images
     ↓
URLs
```

This approach is useful because the required data is already available in a structured JSON format.

# Running the Scraper

## 1. Activate the virtual environment

```bash
source artvenv/bin/activate
```

## 2. Go to the Scrapy project

```bash
cd exercise/artscraper
```

## 3. Check available spiders

```bash
scrapy list
```

Example:

```text
artspider
oldartspider
```

## 4. Run the main spider

```bash
scrapy crawl artspider
```

## 5. Run the old spider

Only if you need to test the previous version:

```bash
scrapy crawl oldartspider
```

# Important Notes

Sotheby's website can change its GraphQL API and JSON response structure.

If the scraper suddenly stops finding lots or URLs, check:

1. GraphQL request
2. Response status
3. JSON response
4. JSON structure
5. Lot fields
6. Auction fields
7. Image fields

Before changing the scraper code, first check whether the GraphQL response has changed.

# Main Files at a Glance

| File | Purpose |
|---|---|
| `artspider.py` | Main scraping logic |
| `oldartspider.py` | Older scraper |
| `items.py` | Defines scraped data structure |
| `pipelines.py` | Processes scraped data |
| `middlewares.py` | Controls requests/responses |
| `settings.py` | Scrapy configuration |
| `scrapy.cfg` | Scrapy project configuration |
| `.env` | Environment variables |
| `CSV files` | Scraped/processed data |

# Project Goal

The goal of this project is to build a reliable Sotheby's scraper that can automatically collect:

- Auction information
- Lot information
- Lot URLs
- Artwork information
- Artist information
- Estimates
- Images
- Descriptions

and store the data in a structured format for further analysis.

# Technology

- **Python**
- **Scrapy**
- **GraphQL**
- **JSON**
- **CSV**
- **Sotheby's website**

# Current Spider

The main spider currently used for development is:

```text
artspider.py
```

The previous version is:

```text
oldartspider.py
```
