import os
import re
import sys
import pickle
import unicodedata

import pandas as pd
from tqdm import tqdm
from bs4 import BeautifulSoup

from helpers.constants import RANK
from helpers.wiki_tree import WikiTree
from helpers.async_utils import run_requests, divide_chunks


__name__ = "taxopedia"


# CONSTANTS


RANK_SET = set(RANK)


# FUNCTIONS


def wd_join(*args):
    return os.path.join(sys.path[0], *args)


def my_normalize(word):
    return unicodedata.normalize("NFKC", word)


def key_sort(key):
    try:
        return RANK.index(key)
    except ValueError:
        return float("inf")


def rank_sort(row):
    return RANK.index(row["Rank"])


def dedupe(iterable, *, key=None):
    seen = set()
    for x in iterable:
        if key:
            proxy = key(x)
        else:
            proxy = x
        if proxy not in seen:
            yield x
            seen.add(proxy)


def load_dict(search_term):
    try:
        with open(search_term + ".search", "rb") as f:
            return pickle.load(f)
    except FileNotFoundError:
        return {
            "root": "https://en.wikipedia.org/wiki/",
            "all": set(),
            "seen": set(),
            "biota": set(),
            "cleaned": set(),
            "search_term": search_term,
        }


def dump_dict(search_term, dict_):
    with open(search_term + ".search", "wb") as f:
        pickle.dump(dict_, f)


def per_result(result, links_dict):
    if result is None:
        return

    # expand result
    url, status, html = result

    # filter out pages:
    # without a biota box
    if "biota" not in html:
        return

    # parse the page
    soup = BeautifulSoup(html, "lxml")

    # find the biota box
    box = soup.select_one(".biota")

    # filter out boxes:
    # without mention of search term
    if box is None or links_dict["search_term"] not in str(box):
        return

    # remove root from extension
    url = url[len(links_dict["root"]):]

    body = soup.select_one("#content")
    links_dict["biota"].add(url)

    to_check = box.select("a")
    if links_dict["comprehensive"]:
        to_check += body.select("a")

    new_links = set()
    for link in to_check:
        href = grab_href(link, links_dict)
        if href:
            new_links.add(href)
    links_dict["all"] |= new_links


def grab_href(tag, links_dict):
    PATTERN = re.compile(r"(/wiki/\w+:)")

    try:
        href = tag["href"]

        # don't clean links twice
        if href in links_dict["cleaned"]:
            return
        links_dict["cleaned"].add(href)

        # make sure it's a wiki page
        if not href.startswith("/wiki/"):
            return

        # don't want files, templates, etc.
        if PATTERN.search(href):
            return
        href = href[6:]

        # add wikipedia, remove section
        # href = "https://en.wikipedia.org" + href
        sublink = href.rfind("#")
        if sublink != -1:
            href = href[:sublink]

        return href
    except KeyError:
        return


def search(search_term, comprehensive=False):
    # keep a dictionary of links
    links_dict = load_dict(search_term)
    links_dict["comprehensive"] = comprehensive

    # if nothing loaded, make sure to start with search term
    links_dict["all"].add(search_term)
    try:
        links_dict["seen"].remove(search_term)
    except KeyError:
        pass

    # find remaining links
    links_dict["remaining"] = links_dict["all"] - links_dict["seen"]

    # loop as long as links still remain
    while any(links_dict["remaining"]):
        urls = list(links_dict["remaining"])

        # how many links do we have to check?
        print("To check:", len(urls))
        for subset in tqdm(list(divide_chunks(urls, 50))):
            # have seen these links
            links_dict["seen"] |= set(subset)

            # add root to extension
            subset = (links_dict["root"] + x for x in subset)
            for result in run_requests(subset):
                per_result(result, links_dict)

            # save the dictionary
            dump_dict(search_term, links_dict)

        # update remaining links
        links_dict["remaining"] = links_dict["all"] - links_dict["seen"]

    # how many links did we check?
    print("Total links:", len(links_dict["seen"]))
    return links_dict


def linker(search_or_dict, filename=None):
    if type(search_or_dict) is dict:
        links_dict = search_or_dict
        search_term = search_or_dict["search_term"]
    else:
        search_term = search_or_dict
        links_dict = load_dict(search_term)

    if not os.path.exists(search_term):
        os.makedirs(search_term)

    urls = [
        "https://en.wikipedia.org/wiki/" + extension
        for extension in links_dict["biota"]
        if not os.path.exists(os.path.join(search_term, extension + ".html"))
    ]

    for subset in tqdm(list(divide_chunks(urls, 50))):
        results = run_requests(subset)

        for url, status, html in results:
            temp_filename = url.split("/")[-1] + ".html"
            # print("Now parsing:", temp_filename)
            soup = BeautifulSoup(html, "lxml")
            text = soup.select_one(".biota")

            with open(os.path.join(search_term, temp_filename), "w", encoding="utf-8") as f:
                f.write(str(text))

    files = os.listdir(os.path.join(search_term))

    htmls = []
    for fname in files:
        with open(os.path.join(search_term, fname), encoding="utf-8") as f:
            html = f.read()
            if "Ancestral taxa" in html:
                os.remove(os.path.join(search_term, fname))
            else:
                htmls.append(html)

    pattern = re.compile(r"(>(.*?)<)")

    data = []
    for html in htmls:
        soup = BeautifulSoup(html, "lxml")

        tbody = soup.find("tbody")
        if not tbody:
            continue

        traits = []
        for x in tbody.children:
            try:
                _, matches = iter(
                    zip(*pattern.findall(" ".join(str(x).split()))))
                matches = map(str.strip, matches)
                traits += iter(map(my_normalize, filter(bool, matches)))
            except AttributeError:
                pass
            except ValueError:
                pass

        try:
            my_index = traits.index("Scientific classification")
        except ValueError:
            my_index = -1

        dictionary = {"Common Name": traits[0]}

        prev = ""
        order = ["Common Name"]

        iterable = iter(range(len(traits)))
        i = next(iterable)
        while True:
            try:
                t = traits[i]
                if "×" in t:
                    traits = (
                        traits[:i - 1] +
                        [" ".join(traits[i - 1:i + 2])] +
                        traits[i + 2:]
                    )
                elif t.startswith("♂") or t.startswith("♀"):
                    traits = (
                        traits[:i - 1] +
                        [" ".join(traits[i - 1:i + 1])] +
                        traits[i + 1:]
                    )
                else:
                    i = next(iterable)
            except StopIteration:
                break
            except IndexError:
                break

        iterator = iter(traits[1 + my_index:])
        for curr in iterator:
            if prev.istitle() and prev.endswith(":"):
                prev = prev.rstrip(":")
                if prev not in RANK_SET:
                    continue

                if curr == "†":
                    curr += next(iterator)
                dictionary[prev] = curr
                order.append(prev)
            prev = curr

        dictionary["Rank"] = order[-1]
        if order[-1] == "Species":
            try:
                sp = dictionary["Species"]
                gn = dictionary["Genus"]
                dictionary["Species"] = sp.replace(gn[0] + ".", gn)
            except KeyError:
                pass
        data.append(dictionary)

    all_keys = sorted(
        set(key for line in data for key in line.keys()),
        key=key_sort
    )

    data.sort(key=rank_sort)

    df = pd.DataFrame(dedupe(data, key=repr))[all_keys]
    if filename is None:
        filename = f"{search_term}.csv"
    df.to_csv(wd_join(filename), index=False)

    return data


def arboretum(taxa: str, comprehensive: bool) -> WikiTree:
    links_dict = search(taxa, comprehensive)

    csv_name = f"{taxa}.csv"
    data = linker(links_dict, filename=csv_name)

    tree = WikiTree.from_csv(csv_name)
    return tree
