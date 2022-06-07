"""FlowController."""
# pylint: disable=unnecessary-lambda,invalid-name,relative-beyond-top-level
import os
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Iterator, List, Optional

import pymongo
from bson.decimal128 import Decimal128
from pymongo.collection import ReturnDocument
from pymongo.errors import AutoReconnect
from pymongo.operations import UpdateOne
from tenacity import retry_if_exception_type, stop_after_attempt, wait_random

from kytos.core import log
from kytos.core.db import Mongo
from kytos.core.retry import before_sleep, for_all_methods, retries

from ..db.models import FlowCheckDoc, FlowDoc


@for_all_methods(
    retries,
    stop=stop_after_attempt(
        int(os.environ.get("MONGO_AUTO_RETRY_STOP_AFTER_ATTEMPT", 3))
    ),
    wait=wait_random(
        min=int(os.environ.get("MONGO_AUTO_RETRY_WAIT_RANDOM_MIN", 0.1)),
        max=int(os.environ.get("MONGO_AUTO_RETRY_WAIT_RANDOM_MAX", 1)),
    ),
    before_sleep=before_sleep,
    retry=retry_if_exception_type((AutoReconnect,)),
)
class FlowController:
    """FlowController."""

    def __init__(self, get_mongo=lambda: Mongo()) -> None:
        """FlowController."""
        self.mongo = get_mongo()
        self.db_client = self.mongo.client
        self.db = self.db_client[self.mongo.db_name]

    def bootstrap_indexes(self) -> None:
        """Bootstrap indexes."""
        index_tuples = [
            ("flows", [("flow_id", pymongo.ASCENDING)], {"unique": True}),
            (
                "flows",
                [
                    ("switch", pymongo.ASCENDING),
                    ("flow.cookie", pymongo.ASCENDING),
                    ("state", pymongo.ASCENDING),
                    ("inserted_at", pymongo.ASCENDING),
                ],
                {},
            ),
            (
                "flow_checks",
                [
                    ("state", pymongo.ASCENDING),
                    ("updated_at", pymongo.ASCENDING),
                ],
                {},
            ),
        ]
        for collection, keys, kwargs in index_tuples:
            if self.mongo.bootstrap_index(collection, keys, **kwargs):
                log.info(f"Created DB index {keys}, collection: {collection})")

    def upsert_flows(self, match_ids: List[str], flow_dicts: List[dict]) -> dict:
        """Update or insert flows."""
        utc_now = datetime.utcnow()
        ops = []
        for match_id, flow_dict in zip(match_ids, flow_dicts):
            model = FlowDoc(
                **{
                    **flow_dict,
                    **{"_id": match_id, "updated_at": utc_now},
                }
            )
            payload = model.dict(exclude={"inserted_at"}, exclude_none=True)
            ops.append(
                UpdateOne(
                    {"_id": match_id},
                    {
                        "$set": payload,
                        "$setOnInsert": {"inserted_at": utc_now},
                    },
                    upsert=True,
                )
            )
        return self.db.flows.bulk_write(ops).upserted_ids

    @staticmethod
    def _set_updated_at(update_expr: dict) -> None:
        """Set updated_at on $set expression."""
        if "$set" in update_expr:
            update_expr["$set"].update({"updated_at": datetime.utcnow()})
        else:
            update_expr.update({"$set": {"updated_at": datetime.utcnow()}})

    def update_flow_state(self, flow_id: str, state: str) -> Optional[dict]:
        """Update flow state."""
        return self._update_flow(flow_id, {"$set": {"state": state}})

    def _update_flow(self, flow_id: str, update_expr: dict) -> Optional[dict]:
        """Try to find one flow and update it given an update expression."""
        self._set_updated_at(update_expr)
        return self.db.flows.find_one_and_update(
            {"flow_id": flow_id},
            update_expr,
            return_document=ReturnDocument.AFTER,
        )

    def update_flows_state(self, flow_ids: List[str], state: str) -> int:
        """Bulk update flows state."""
        return self.db.flows.update_many(
            {"flow_id": {"$in": flow_ids}}, {"$set": {"state": state}}
        ).modified_count

    def delete_flows_by_ids(self, flow_ids: List[str]) -> int:
        """Delete flows by ids."""
        return self.db.flows.delete_many({"flow_id": {"$in": flow_ids}}).deleted_count

    def delete_flow_by_id(self, flow_id: str) -> int:
        """Delete flow by id."""
        return self.db.flows.delete_one({"flow_id": flow_id}).deleted_count

    def get_flows_lte_inserted_at(self, dpid: str, dt: datetime) -> Iterator[dict]:
        """Get flows less than or equal inserted_at."""
        for flow in self.db.flows.find(
            {"switch": dpid, "inserted_at": {"$lte": dt}}
        ).sort("inserted_at", pymongo.ASCENDING):
            flow["flow"]["cookie"] = int(flow["flow"]["cookie"].to_decimal())
            yield flow

    def get_flows(self, dpid: str) -> Iterator[dict]:
        """Get flows."""
        for flow in self.db.flows.find({"switch": dpid}):
            flow["flow"]["cookie"] = int(flow["flow"]["cookie"].to_decimal())
            yield flow

    def get_flows_by_cookie(self, dpid: str, cookie: int) -> Iterator[dict]:
        """Get flows by cookie."""
        for flow in self.db.flows.find(
            {"switch": dpid, "flow.cookie": Decimal128(Decimal(cookie))}
        ):
            flow["flow"]["cookie"] = int(flow["flow"]["cookie"].to_decimal())
            yield flow

    def get_flows_by_cookies(self, dpids: List[str], cookies: List[int]) -> dict:
        """Get flows by cookies grouped by dpid."""
        flows = defaultdict(list)
        for document in self.db.flows.aggregate(
            [
                {
                    "$match": {
                        "switch": {"$in": dpids},
                        "flow.cookie": {
                            "$in": [Decimal128(Decimal(cookie)) for cookie in cookies]
                        },
                    }
                },
                {"$group": {"_id": "$switch", "flows": {"$push": "$$ROOT"}}},
            ]
        ):
            for flow in document["flows"]:
                flow["flow"]["cookie"] = int(flow["flow"]["cookie"].to_decimal())
                flows[flow["switch"]].append(flow)
        return flows

    def get_flows_by_state(self, dpid: str, state: str) -> Iterator[dict]:
        """Get flows by state."""
        for flow in self.db.flows.find({"switch": dpid, "state": state}):
            flow["flow"]["cookie"] = int(flow["flow"]["cookie"].to_decimal())
            yield flow

    def upsert_flow_check(self, dpid: str, state="active") -> Optional[dict]:
        """Update or insert flow check."""
        utc_now = datetime.utcnow()
        model = FlowCheckDoc(**{"_id": dpid, "state": state, "updated_at": utc_now})
        updated = self.db.flow_checks.find_one_and_update(
            {"_id": dpid},
            {
                "$set": model.dict(exclude={"inserted_at"}),
                "$setOnInsert": {"inserted_at": utc_now},
            },
            return_document=ReturnDocument.AFTER,
            upsert=True,
        )
        return updated

    def get_flow_check_gte_updated_at(
        self,
        dpid: str,
        dt: datetime,
        state="active",
    ) -> Optional[dict]:
        """Get flow check greater than or equal updated_at."""
        return self.db.flow_checks.find_one(
            {"_id": dpid, "state": state, "updated_at": {"$gte": dt}}
        )