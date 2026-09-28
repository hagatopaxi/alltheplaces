from typing import Any, AsyncIterator, Iterable
from urllib.parse import urlencode

from scrapy import Request
from scrapy.http import JsonRequest, TextResponse

from locations.categories import Categories, apply_category
from locations.geo import country_iseadgg_centroids
from locations.items import Feature
from locations.storefinders.yext_search import YextSearchSpider

LA_POSTE = {"operator": "La Poste", "operator_wikidata": "Q373724"}

# Sites operated by La Poste or by a local authority on its behalf are post
# offices in their own right.
POST_OFFICES = {
    "La Poste",
    "La Poste Agence Communale",
    "La Poste Agence Intercommunale",
}

# Shops (bakers, tobacconists, …) hosting a postal counter. Their own trade is
# not published, so they get amenity=yes like other post partners in ATP.
POST_PARTNERS = {
    "La Poste Relais",
    "La Poste Point partenaire",
}


class LaPosteFRSpider(YextSearchSpider):
    name = "la_poste_fr"
    host = "https://localiser.laposte.fr"
    # The search answers at most 10,000 results per query, fewer than La Poste
    # has sites: it is run around each 158 km centroid, 100 miles being the
    # radius it takes. The widest circle, around Paris, holds 3,400 sites.
    search_radius_miles = 100

    def make_request(self, offset: int, centre: str = "") -> JsonRequest:
        query = urlencode(
            {
                "q": centre,
                "r": self.search_radius_miles,
                "per": self.page_size,
                "offset": offset,
                # Post offices and postal counters: no parcel lockers, pickup
                # points, letter boxes or business-only sites.
                "jesuis": "particulier",
                "contact": "vente",
            }
        )
        return JsonRequest(f"{self.host}/index.html?{query}", headers={"Accept": "application/json"})

    async def start(self) -> AsyncIterator[Request]:
        for lat, lon in country_iseadgg_centroids(["FR", "AD"], 158):
            yield self.make_request(0, f"{lat},{lon}")

    def request_next_page(self, response: TextResponse, **kwargs: Any) -> Iterable[JsonRequest]:
        pager = response.json()["queryParams"]
        offset = int(pager["offset"][0]) + self.page_size
        if offset < response.json()["response"]["count"]:
            yield self.make_request(offset, pager["q"][0])

    def parse_item(self, location: dict, item: Feature) -> Iterable[Feature]:
        profile = location["profile"]
        if profile.get("closed"):
            return
        if not (ref := profile.get("c_codeACORES")):
            self.crawler.stats.inc_value("atp/la_poste_fr/no_ref")
            return

        # The site code of La Poste's open data, which OSM carries too.
        item["ref"] = item["extras"]["ref:FR:LaPoste"] = ref
        item["branch"] = profile.get("c_intituléÉtablissement")
        # La Poste's own position, the one its open data publishes: Yext's
        # geocoding of the address lands kilometres away on 3% of the sites.
        if coordinate := profile.get("displayCoordinate"):
            item["lat"], item["lon"] = coordinate["lat"], coordinate["long"]
        item.pop("name", None)
        # 3631 or its 09 70 82 36 31 equivalent, La Poste's national number.
        item.pop("phone", None)
        if not item["opening_hours"].as_opening_hours():
            item.pop("opening_hours")

        # An unrecognised type falls through to the partner tagging, which
        # claims less than amenity=post_office does.
        site_type = profile.get("c_typeÉtablissement1")
        if site_type in POST_OFFICES:
            apply_category(Categories.POST_OFFICE, item)
            item.update(LA_POSTE)
        else:
            if site_type not in POST_PARTNERS:
                self.logger.error("Unexpected site type: {}".format(site_type))
            apply_category(Categories.GENERIC_POI, item)
            item["extras"]["post_office"] = "post_partner"
            item["extras"]["post_office:brand"] = "La Poste"
            item["extras"]["post_office:brand:wikidata"] = "Q373724"
            # The page is La Poste's counter, not the shop's own website.
            item.pop("website", None)

        yield item
