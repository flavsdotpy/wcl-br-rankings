import hashlib
from datetime import datetime, timedelta

from api import WCLApiClient
from cfg import ReportConfig
from db import DBClient
from log import get_logger

RAID_SIZE = 25
METRICS_CHECKED = ["dps"]

class WCLBrazilReport:

    def __init__(self):
        self.__cfg = ReportConfig()
        self.__api_client = WCLApiClient()
        self.__db = DBClient()

    def __fetch_character_parses(self, character: dict, metric="dps"):
        get_logger().debug(
            f"Fetching {metric} rankings for {character['name']} {character['realm']}-{character['region']}")
        character_rankings = list()
        try:
            for zone_id, zone in self.__cfg.zones.items():
                rankings = self.__api_client.get_character_rankings(
                    character["name"], character["realm"],  character["region"],
                    params={"metric": metric, "zone": zone_id}
                )
                for ranking in rankings:
                    parse_id = hashlib.md5(
                        f"{ranking['reportID']}.{ranking['fightID']}.{ranking['characterID']}.{metric}".encode()
                    ).hexdigest()
                    if parse_id in self.__cfg.processed_parses:
                        get_logger().debug("Parse already processed...")
                        continue

                    if ranking["size"] != RAID_SIZE:
                        get_logger().debug(f"Not considering raid size {ranking['size']}")
                        continue

                    encounter = self.__cfg.encounters.get(ranking["encounterID"])
                    if not encounter or encounter["zone_name"] != zone["name"]:
                        get_logger().debug(f"Encounter {ranking['encounterName']} is invalid for zone {zone['name']}")
                        continue

                    if encounter["difficulty"] != ranking["difficulty"]:
                        get_logger().debug(f"Not considering difficulty {ranking['difficulty']} "
                                           f"for encounter {ranking['encounterName']}")
                        continue

                    if metric == "dps":
                        if ranking["spec"] not in self.__cfg.classes[ranking["class"]]["specs"]["dps"]:
                            get_logger().debug(f"Not condidering spec {ranking['spec']} "
                                               f"for class {ranking['class']} in metric {metric}")
                            continue

                    report = self.__fetch_report(ranking["reportID"], self.__cfg.guilds[character["guild"]])
                    if not report or ranking["fightID"] not in report["fights"]:
                        get_logger().debug("Report or fight not in guilds reports list! Skipping ranking...")
                        continue

                    character_rankings.append({
                        "id": parse_id,
                        "character_id": character["id"],
                        "name": character["name"],
                        "class": character["class"],
                        "spec": ranking["spec"],
                        "realm": character["realm"],
                        "region": character["region"],
                        "guild": character["guild"],
                        "faction": character["faction"],
                        "zone": zone["name"],
                        "zone_id": zone_id,
                        "encounter": ranking["encounterName"],
                        "encounter_id": ranking["encounterID"],
                        "duration": self.__reports[character["guild"]][ranking["reportID"]]["fights"]
                                                        [ranking["fightID"]]["duration"],
                        "percentile": ranking["percentile"],
                        "metric": metric,
                        "value": ranking["total"],
                        "ilvl": ranking["ilvlKeyOrPatch"],
                        "date": report["date"],
                    })
        except Exception as e:
            get_logger().error(f"Something happened: {str(e)}")

        get_logger().info(f"Found {len(character_rankings)} new parses for "
                          f"character {character['name']} {character['realm']}-{character['region']}")
        return character_rankings

    def __fetch_new_characters_by_guild(self, guild_name: str, guild: dict):
        get_logger().info(f"Fetching characters from API for guild: {guild_name}")
        guild_characters = dict()

        for report_id, report in self.__reports[guild_name].items():
            for character_name, character in report["characters"].items():
                if character["id"] in self.__cfg.known_characters:
                    continue
                new_char_obj = {
                    "id": character["id"],
                    "name": character["name"],
                    "guild": report["guild"],
                    "realm": report["realm"],
                    "region": report["region"],
                    "faction": report["faction"],
                    "class": character["class"],
                    "is_blacklisted": False
                }
                guild_characters[new_char_obj["id"]] = new_char_obj

        get_logger().info(f"Found {len(guild_characters)} new characters for guild {guild_name}")
        return guild_characters

    def __fetch_report(self, report_id: str, guild: dict):
        if report_id in self.__reports[guild["name"]]:
            return self.__reports[guild["name"]][report_id]

        try:
            report_info = self.__api_client.get_report_info(report_id)
            characters = {
                c["name"]: c
                for c in report_info["exportedCharacters"]
            }
            for friendly in report_info["friendlies"]:
                if friendly["name"] in characters:
                    characters[friendly["name"]]["class"] = friendly["type"]

            report = {
                "id": report_id,
                "title": report_info["title"],
                "date": datetime.fromtimestamp(report_info["start"] / 1000),
                "guild": guild["name"],
                "realm": guild["realm"],
                "region": guild["region"],
                "faction": guild["faction"],
                "characters": characters,
                "fights": dict(),
            }
            for fight in report_info["fights"]:
                if fight.get("kill", False) and fight.get("size") == RAID_SIZE:
                    report["fights"][fight["id"]] = {
                        "duration": (fight["end_time"] - fight["start_time"]) / 1000
                    }

            get_logger().info(f"Found new report {report_id} for guild {guild['name']}")
            self.__reports[guild["name"]][report_id] = report
            return report
        except Exception as e:
            get_logger().error(f"Something happened when fetching reports: {str(e)}")
            return None

    def __fetch_reports_by_guild(self, guild_name: str, guild: dict):
        start_date = (datetime.now() - timedelta(days=5)).timestamp() * 1000
        late_guild_reports = self.__api_client.get_guild_reports(
            guild["name"], guild["realm"], guild["region"], params={"start": start_date}
        )
        for report_entry in late_guild_reports:
            if report_entry["id"] not in self.__cfg.processed_reports:
                self.__fetch_report(report_entry["id"], guild)

    def __load_reports(self):
        get_logger().info("Loading reports...")
        self.__reports = dict()
        for guild_name, guild in self.__cfg.guilds.items():
            self.__reports[guild_name] = dict()
            self.__fetch_reports_by_guild(guild_name, guild)

    def __load_characters(self):
        get_logger().info("Loading characters...")
        self.__characters = dict()
        self.__characters.update(self.__cfg.known_characters)
        for guild_name, guild in self.__cfg.guilds.items():
            guild_new_characters = self.__fetch_new_characters_by_guild(guild_name, guild)
            self.__characters.update(guild_new_characters)

    def __load_parses(self):
        get_logger().info("Loading characters parses...")
        self.__parses = dict()
        for metric in METRICS_CHECKED:
            get_logger().info(f"Loading characters {metric} parses...")
            self.__parses[metric] = list()
            for character_id, character in self.__characters.items():
                character_parses = self.__fetch_character_parses(character, metric)
                if character_parses:
                    self.__parses[metric].extend(character_parses)

    def __save_reports(self):
        get_logger().info("Preparing to save reports...")
        reports_to_save = list()
        for guild_name, reports in self.__reports.items():
            for report_id, report in reports.items():
                if report_id not in self.__cfg.processed_reports:
                    reports_to_save.append(report)
        get_logger().info(f"Saving {len(reports_to_save)} reports")
        self.__db.insert_reports(reports_to_save)

    def __save_parses(self):
        get_logger().info("Preparing to save parses...")
        for metric, parses in self.__parses.items():
            parses_to_save = [parse for parse in parses if parse["id"] not in self.__cfg.processed_parses]
            get_logger().info(f"Saving {len(parses)} parses for metric {metric}...")
            self.__db.insert_parses(parses_to_save)

    def __save_characters(self):
        get_logger().info("Preparing to save characters...")
        characters_to_save = list()
        for character_id, character in self.__characters.items():
            if character_id not in self.__cfg.known_characters:
                characters_to_save.append(character)
        get_logger().info(f"Saving {len(characters_to_save)} characters...")
        self.__db.insert_characters(characters_to_save)

    def load(self):
        get_logger().info("Loading data...")
        self.__load_reports()
        self.__load_characters()
        self.__load_parses()
        return self

    def calculate_rankings(self):
        get_logger().info("Generating rankings...")
        # do report logic
        return self

    def save(self):
        self.__save_reports()
        self.__save_parses()
        self.__save_characters()
        return self
