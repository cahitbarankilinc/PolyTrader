from __future__ import annotations

import csv
import json
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from time import time

from .client import PolymarketClient
from .config import ScannerConfig
from .database import ScannerDatabase
from .models import BucketStat, CandidateWallet, WalletScanResult, WalletWinStats


def normalize_win_stats_payload(win_stats: dict | None) -> dict:
    payload = dict(win_stats or {})
    payload.setdefault('wins', 0)
    payload.setdefault('losses', 0)
    payload.setdefault('win_rate', 0.0)
    payload.setdefault('grouped_buckets', [BucketStat(label=label, start_cents=start, end_cents=end).to_dict() for (label, start, end) in GROUPED_BUCKETS])
    analyzed_closed_positions = int(payload.get('analyzed_closed_positions') or 0)
    analyzed_open_loss_positions = int(payload.get('analyzed_open_loss_positions') or 0)
    payload['analyzed_closed_positions'] = analyzed_closed_positions
    payload['analyzed_open_loss_positions'] = analyzed_open_loss_positions
    payload['analyzed_positions'] = int(payload.get('analyzed_positions') or (analyzed_closed_positions + analyzed_open_loss_positions))

    normalized_outcome_stats: dict[str, dict] = {}
    for key, value in (payload.get('outcome_stats') or {}).items():
        item = dict(value or {})
        item.setdefault('wins', 0)
        item.setdefault('losses', 0)
        item.setdefault('win_rate', 0.0)
        item.setdefault('grouped_buckets', [BucketStat(label=label, start_cents=start, end_cents=end).to_dict() for (label, start, end) in GROUPED_BUCKETS])
        item_closed = int(item.get('analyzed_closed_positions') or 0)
        item_open = int(item.get('analyzed_open_loss_positions') or 0)
        item['analyzed_closed_positions'] = item_closed
        item['analyzed_open_loss_positions'] = item_open
        item['analyzed_positions'] = int(item.get('analyzed_positions') or (item_closed + item_open))
        normalized_outcome_stats[key] = item
    payload['outcome_stats'] = normalized_outcome_stats
    return payload


GROUPED_BUCKETS = [
    ('0-15¢', 0, 15),
    ('15-35¢', 15, 35),
    ('35-65¢', 35, 65),
    ('65-85¢', 65, 85),
    ('85-100¢', 85, 101),
]
OPEN_POSITION_LOSS_MIN = -101.0
OPEN_POSITION_LOSS_MAX = -95.0


class WeatherWalletScanner:
    def __init__(self, config: ScannerConfig | None = None) -> None:
        self.config = config or ScannerConfig()
        self.config.ensure_directories()
        self.client = PolymarketClient(self.config.gamma_base, self.config.data_base)
        self.db = ScannerDatabase(self.config.db_path)
        self.db.init()
        self._state_lock = Lock()
        self._db_write_lock = Lock()

    def _write_scan_state(self, payload: dict) -> None:
        with self._state_lock:
            self.config.scan_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.config.scan_state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def _save_result_threadsafe(self, scan_id: int, result: WalletScanResult) -> None:
        with self._db_write_lock:
            self.db.save_result(scan_id, result)

    def read_scan_state(self) -> dict:
        if not self.config.scan_state_path.exists():
            return {'running': False}
        try:
            return json.loads(self.config.scan_state_path.read_text(encoding='utf-8'))
        except Exception:
            return {'running': False}

    def _scan_state_payload(
        self,
        *,
        running: bool,
        scan_id: int | None = None,
        total_candidates: int = 0,
        completed_candidates: int = 0,
        active_category: str | None = None,
        category_totals: dict[str, int] | None = None,
        category_completed: dict[str, int] | None = None,
        completed_categories: list[str] | None = None,
        errors: list[str] | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
    ) -> dict:
        percent = int((completed_candidates / total_candidates) * 100) if total_candidates else 0
        if total_candidates and completed_candidates and percent == 0:
            percent = 1
        return {
            'running': running,
            'scan_id': scan_id,
            'total_candidates': total_candidates,
            'completed_candidates': completed_candidates,
            'percent': percent,
            'active_category': active_category,
            'category_totals': category_totals or {},
            'category_completed': category_completed or {},
            'completed_categories': completed_categories or [],
            'errors': errors or [],
            'started_at': started_at,
            'finished_at': finished_at,
        }

    def discover_weather_market_terms(self) -> tuple[set[str], set[int]]:
        discovered: set[str] = set(self.config.weather_keywords)
        event_ids: set[int] = set()
        for keyword in self.config.weather_keywords:
            payload = self.client.public_search(keyword, limit_per_type=25, page=1)
            for event in payload.get('events') or []:
                try:
                    event_ids.add(int(event.get('id')))
                except (TypeError, ValueError):
                    pass
                for value in (event.get('title'), event.get('slug'), event.get('category'), event.get('subcategory')):
                    if value:
                        discovered.add(str(value).lower())
                for market in event.get('markets') or []:
                    for value in (market.get('question'), market.get('slug')):
                        if value:
                            discovered.add(str(value).lower())
        return discovered, event_ids

    def seed_leaderboard_candidates(self) -> dict[str, list[CandidateWallet]]:
        grouped: dict[str, dict[str, CandidateWallet]] = {category.lower(): {} for category in self.config.leaderboard_categories}
        for category in self.config.leaderboard_categories:
            category_key = category.lower()
            for period in self.config.leaderboard_periods:
                for offset in self.config.leaderboard_offsets:
                    try:
                        rows = self.client.leaderboard(
                            category=category,
                            time_period=period,
                            order_by=self.config.leaderboard_order_by,
                            limit=self.config.leaderboard_limit,
                            offset=offset,
                        )
                    except Exception:
                        continue
                    if not rows:
                        continue
                    for row in rows:
                        address = str(row.get('proxyWallet') or '').lower()
                        if not address:
                            continue
                        existing = grouped[category_key].get(address)
                        candidate = CandidateWallet(
                            address=address,
                            username=row.get('userName'),
                            pnl=float(row.get('pnl') or 0.0),
                            volume=float(row.get('vol') or 0.0),
                            source=f'leaderboard:{period.lower()}',
                            source_category=category_key,
                            verified_badge=bool(row.get('verifiedBadge')),
                        )
                        if existing is None or (candidate.pnl or 0.0) > (existing.pnl or 0.0):
                            grouped[category_key][address] = candidate
        return {category: list(items.values()) for category, items in grouped.items()}

    def seed_event_trade_candidates(self, weather_event_ids: set[int], seen_addresses: set[str]) -> list[CandidateWallet]:
        candidates: dict[str, CandidateWallet] = {}
        for event_id in sorted(weather_event_ids)[: self.config.max_seed_events]:
            try:
                trades = self.client.trades(event_id=event_id, limit=self.config.seed_event_trade_limit)
            except Exception:
                continue
            for trade in trades:
                address = str(trade.get('proxyWallet') or '').lower()
                if not address or address in seen_addresses or address in candidates:
                    continue
                candidates[address] = CandidateWallet(
                    address=address,
                    username=trade.get('name') or trade.get('pseudonym'),
                    pnl=None,
                    volume=None,
                    source=f'event_trades:{event_id}',
                    source_category='weather',
                    verified_badge=None,
                )
        return list(candidates.values())

    def seed_custom_candidates(self, seen_addresses: set[str]) -> list[CandidateWallet]:
        candidates: list[CandidateWallet] = []
        for address in self.db.list_custom_wallets():
            if address in seen_addresses:
                continue
            candidates.append(
                CandidateWallet(
                    address=address,
                    username=None,
                    pnl=None,
                    volume=None,
                    source='custom_wallet',
                    source_category='custom',
                    verified_badge=None,
                )
            )
        return candidates

    @staticmethod
    def _matches_weather(activity: dict, weather_terms: set[str]) -> bool:
        haystacks = [
            str(activity.get('title') or '').lower(),
            str(activity.get('slug') or '').lower(),
            str(activity.get('eventSlug') or '').lower(),
            str(activity.get('outcome') or '').lower(),
        ]
        return any(term in hay for hay in haystacks for term in weather_terms if term)

    @staticmethod
    def _price_to_cents(price: float | int | str | None) -> int:
        value = float(price or 0.0)
        cents = int(value * 100)
        return max(0, min(100, cents))

    @staticmethod
    def _is_open_position_loss(row: dict) -> bool:
        try:
            percent_pnl = float(row.get('percentPnl'))
        except (TypeError, ValueError):
            return False
        return OPEN_POSITION_LOSS_MIN <= percent_pnl <= OPEN_POSITION_LOSS_MAX

    @staticmethod
    def _normalized_outcome(row: dict) -> str:
        return str(row.get('outcome') or '').strip()

    @staticmethod
    def _ensure_outcome_bucket_state(
        outcome_key: str,
        outcome_label: str,
        outcome_grouped: dict[str, list[BucketStat]],
        outcome_counts: dict[str, dict[str, int]],
        outcome_labels: dict[str, str],
    ) -> None:
        if outcome_key in outcome_grouped:
            return
        outcome_grouped[outcome_key] = [BucketStat(label=label, start_cents=start, end_cents=end) for (label, start, end) in GROUPED_BUCKETS]
        outcome_counts[outcome_key] = {'wins': 0, 'losses': 0, 'open_losses': 0}
        outcome_labels[outcome_key] = outcome_label

    @staticmethod
    def _record_bucket_result(groups: list[BucketStat], cents: int, is_win: bool) -> None:
        for group in groups:
            if group.start_cents <= cents < group.end_cents:
                group.total += 1
                if is_win:
                    group.wins += 1
                else:
                    group.losses += 1
                break

    def calculate_win_stats(
        self,
        candidate: CandidateWallet,
        rows: list[dict] | None = None,
        open_rows: list[dict] | None = None,
    ) -> WalletWinStats:
        rows = rows if rows is not None else self.client.closed_positions_all(candidate.address, max_items=self.config.closed_positions_limit)
        if open_rows is None:
            try:
                open_rows = self.client.positions(
                    candidate.address,
                    limit=500,
                    sort_by='CURRENT',
                    sort_direction='asc',
                )
            except Exception:
                open_rows = []

        grouped = [BucketStat(label=label, start_cents=start, end_cents=end) for (label, start, end) in GROUPED_BUCKETS]
        wins = 0
        losses = 0
        outcome_grouped: dict[str, list[BucketStat]] = {}
        outcome_counts: dict[str, dict[str, int]] = {}
        outcome_labels: dict[str, str] = {}

        for row in rows:
            cents = self._price_to_cents(row.get('avgPrice'))
            is_win = float(row.get('realizedPnl') or 0.0) > 0.0
            outcome = self._normalized_outcome(row)
            if is_win:
                wins += 1
            else:
                losses += 1

            self._record_bucket_result(grouped, cents, is_win)

            if outcome:
                outcome_key = outcome.lower()
                self._ensure_outcome_bucket_state(outcome_key, outcome, outcome_grouped, outcome_counts, outcome_labels)
                if is_win:
                    outcome_counts[outcome_key]['wins'] += 1
                else:
                    outcome_counts[outcome_key]['losses'] += 1
                self._record_bucket_result(outcome_grouped[outcome_key], cents, is_win)

        analyzed_open_loss_positions = 0
        for row in open_rows:
            if not self._is_open_position_loss(row):
                continue
            analyzed_open_loss_positions += 1
            losses += 1
            cents = self._price_to_cents(row.get('avgPrice'))
            outcome = self._normalized_outcome(row)
            self._record_bucket_result(grouped, cents, False)
            if outcome:
                outcome_key = outcome.lower()
                self._ensure_outcome_bucket_state(outcome_key, outcome, outcome_grouped, outcome_counts, outcome_labels)
                outcome_counts[outcome_key]['losses'] += 1
                outcome_counts[outcome_key]['open_losses'] += 1
                self._record_bucket_result(outcome_grouped[outcome_key], cents, False)

        total = wins + losses
        outcome_stats = {}
        for outcome_key, buckets in outcome_grouped.items():
            counts = outcome_counts[outcome_key]
            outcome_total = counts['wins'] + counts['losses']
            outcome_stats[outcome_key] = {
                'outcome': outcome_labels[outcome_key],
                'analyzed_positions': outcome_total,
                'analyzed_closed_positions': outcome_total - counts.get('open_losses', 0),
                'analyzed_open_loss_positions': counts.get('open_losses', 0),
                'wins': counts['wins'],
                'losses': counts['losses'],
                'win_rate': (counts['wins'] / outcome_total) if outcome_total else 0.0,
                'grouped_buckets': [bucket.to_dict() for bucket in buckets],
            }

        return WalletWinStats(
            analyzed_positions=total,
            analyzed_closed_positions=total - analyzed_open_loss_positions,
            analyzed_open_loss_positions=analyzed_open_loss_positions,
            wins=wins,
            losses=losses,
            win_rate=(wins / total) if total else 0.0,
            grouped_buckets=[bucket.to_dict() for bucket in grouped],
            outcome_stats=outcome_stats,
        )

    def build_seed_result(self, candidate: CandidateWallet) -> WalletScanResult:
        return WalletScanResult(
            address=candidate.address,
            username=candidate.username,
            pnl=float(candidate.pnl) if candidate.pnl is not None else 0.0,
            last_trade_count=0,
            sell_trade_count=0,
            buy_trade_count=0,
            qualified=False,
            qualification_reason='seed_only_pending_deep_scan',
            source=candidate.source,
            source_category=candidate.source_category,
            win_stats=WalletWinStats(
                analyzed_positions=0,
                analyzed_closed_positions=0,
                analyzed_open_loss_positions=0,
                wins=0,
                losses=0,
                win_rate=0.0,
                grouped_buckets=[BucketStat(label=label, start_cents=start, end_cents=end).to_dict() for (label, start, end) in GROUPED_BUCKETS],
            ).to_dict(),
        )

    def _fetch_candidate_inputs(self, candidate: CandidateWallet) -> dict:
        with ThreadPoolExecutor(max_workers=self.config.per_wallet_fetch_workers) as executor:
            futures: dict[str, Future] = {
                'distinct_markets': executor.submit(self.client.total_markets_traded, candidate.address),
                'activity': executor.submit(self.client.user_activity, candidate.address, self.config.activity_limit),
                'closed_positions': executor.submit(self.client.closed_positions_all, candidate.address, self.config.closed_positions_limit),
                'open_positions': executor.submit(
                    self.client.positions,
                    candidate.address,
                    500,
                    0,
                    'CURRENT',
                    'asc',
                ),
            }
            result: dict[str, object] = {}
            for key, future in futures.items():
                try:
                    result[key] = future.result()
                except Exception:
                    if key == 'open_positions':
                        result[key] = []
                    else:
                        raise
            return result

    def evaluate_candidate(self, candidate: CandidateWallet, weather_terms: set[str]) -> WalletScanResult:
        fetched = self._fetch_candidate_inputs(candidate)
        distinct_markets = int(fetched['distinct_markets'])
        activity = list(fetched['activity'])
        trade_rows = [row for row in activity if row.get('type') == 'TRADE']
        sell_trade_count = sum(1 for row in trade_rows if str(row.get('side') or '').upper() == 'SELL')
        buy_trade_count = sum(1 for row in trade_rows if str(row.get('side') or '').upper() == 'BUY')
        last_trade_count = len(trade_rows)
        closed_positions = list(fetched['closed_positions'])
        open_positions = list(fetched.get('open_positions') or [])

        realized_pnl = sum(float(row.get('realizedPnl') or 0.0) for row in closed_positions)
        open_cash_pnl = sum(float(row.get('cashPnl') or 0.0) for row in open_positions)
        if candidate.pnl is not None:
            pnl_value = float(candidate.pnl)
        elif closed_positions:
            pnl_value = realized_pnl
        elif open_positions:
            pnl_value = open_cash_pnl
        else:
            pnl_value = 0.0

        username = candidate.username
        if not username:
            username = next((str(row.get('name') or row.get('pseudonym') or '').strip() for row in trade_rows if (row.get('name') or row.get('pseudonym'))), None) or None
        win_stats = self.calculate_win_stats(candidate, rows=closed_positions, open_rows=open_positions)

        qualified = True
        reasons: list[str] = []
        if distinct_markets < self.config.minimum_distinct_markets:
            qualified = False
            reasons.append(f'distinct_markets<{self.config.minimum_distinct_markets}')
        if sell_trade_count > 0:
            qualified = False
            reasons.append('contains_sell_trade')
        if pnl_value is None or pnl_value <= self.config.minimum_positive_pnl:
            qualified = False
            reasons.append('non_positive_pnl')
        if not trade_rows:
            qualified = False
            reasons.append('no_trade_rows')

        weather_trade_count = sum(1 for row in trade_rows if self._matches_weather(row, weather_terms))
        weather_trade_ratio = weather_trade_count / last_trade_count if last_trade_count else 0.0

        return WalletScanResult(
            address=candidate.address,
            username=username,
            pnl=pnl_value,
            distinct_markets_traded=distinct_markets,
            last_trade_count=last_trade_count,
            sell_trade_count=sell_trade_count,
            buy_trade_count=buy_trade_count,
            weather_trade_count=weather_trade_count,
            weather_trade_ratio=weather_trade_ratio,
            qualified=qualified,
            qualification_reason='qualified' if qualified else ','.join(reasons),
            source=candidate.source,
            source_category=candidate.source_category,
            win_stats=win_stats.to_dict(),
        )

    def scan(self) -> list[WalletScanResult]:
        started_at = time()
        scan_id = self.db.create_scan()
        self._write_scan_state(
            self._scan_state_payload(
                running=True,
                scan_id=scan_id,
                total_candidates=0,
                completed_candidates=0,
                active_category='seeding',
                category_totals={},
                category_completed={},
                completed_categories=[],
                errors=[],
                started_at=started_at,
            )
        )
        weather_terms, weather_event_ids = self.discover_weather_market_terms()
        leaderboard_groups = self.seed_leaderboard_candidates()

        seen_addresses = {candidate.address for candidates in leaderboard_groups.values() for candidate in candidates}
        event_candidates = self.seed_event_trade_candidates(weather_event_ids, seen_addresses)
        seen_addresses.update(candidate.address for candidate in event_candidates)
        custom_candidates = self.seed_custom_candidates(seen_addresses)

        stage2_groups: list[tuple[str, list[CandidateWallet]]] = [
            *[(f'{category}_deep', sorted(candidates, key=lambda item: item.pnl or 0.0, reverse=True)) for category, candidates in leaderboard_groups.items()],
            ('weather_deep', event_candidates),
            ('custom_deep', custom_candidates),
        ]

        category_totals = {category: len(candidates) for category, candidates in stage2_groups}
        total_candidates = sum(category_totals.values())
        category_completed = {category: 0 for category in category_totals}
        completed_categories: list[str] = []
        errors: list[str] = []
        completed_candidates = 0
        results: list[WalletScanResult] = []

        self._write_scan_state(
            self._scan_state_payload(
                running=True,
                scan_id=scan_id,
                total_candidates=total_candidates,
                completed_candidates=0,
                active_category=stage2_groups[0][0] if stage2_groups else None,
                category_totals=category_totals,
                category_completed=category_completed,
                completed_categories=completed_categories,
                errors=errors,
                started_at=started_at,
            )
        )

        try:
            for category, candidates in stage2_groups:
                self._write_scan_state(
                    self._scan_state_payload(
                        running=True,
                        scan_id=scan_id,
                        total_candidates=total_candidates,
                        completed_candidates=completed_candidates,
                        active_category=category,
                        category_totals=category_totals,
                        category_completed=category_completed,
                        completed_categories=completed_categories,
                        errors=errors,
                        started_at=started_at,
                    )
                )
                if not candidates:
                    completed_categories.append(category)
                    continue
                with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
                    futures = [executor.submit(self.evaluate_candidate, candidate, weather_terms) for candidate in candidates]
                    for future in as_completed(futures):
                        try:
                            result = future.result()
                            self._save_result_threadsafe(scan_id, result)
                            results.append(result)
                        except Exception as exc:  # noqa: BLE001
                            errors.append(f'{category}: {exc}')
                        completed_candidates += 1
                        category_completed[category] = category_completed.get(category, 0) + 1
                        self._write_scan_state(
                            self._scan_state_payload(
                                running=True,
                                scan_id=scan_id,
                                total_candidates=total_candidates,
                                completed_candidates=completed_candidates,
                                active_category=category,
                                category_totals=category_totals,
                                category_completed=category_completed,
                                completed_categories=completed_categories,
                                errors=errors[-20:],
                                started_at=started_at,
                            )
                        )
                completed_categories.append(category)

            self.db.prune_rejected_history(current_scan_id=scan_id)
            return sorted(results, key=lambda item: (item.qualified, item.pnl or 0.0), reverse=True)
        finally:
            self._write_scan_state(
                self._scan_state_payload(
                    running=False,
                    scan_id=scan_id,
                    total_candidates=total_candidates,
                    completed_candidates=completed_candidates,
                    active_category=None,
                    category_totals=category_totals,
                    category_completed=category_completed,
                    completed_categories=completed_categories,
                    errors=errors[-20:],
                    started_at=started_at,
                    finished_at=time(),
                )
            )

    def analyze_wallet(self, address: str, source: str = 'custom_wallet') -> dict:
        weather_terms, _ = self.discover_weather_market_terms()
        candidate = CandidateWallet(address=address.lower(), username=None, pnl=None, volume=None, source=source, source_category='custom', verified_badge=None)
        result = self.evaluate_candidate(candidate, weather_terms)
        payload = result.to_dict()
        payload['win_stats'] = normalize_win_stats_payload(payload.get('win_stats'))
        self.db.add_custom_wallet(address.lower())
        self.db.save_custom_wallet_result(payload)
        return payload

    def latest_results(self, qualified_only: bool = False, limit: int = 100) -> list[dict]:
        rows = [json.loads(row['payload_json']) for row in self.db.latest_results(qualified_only=qualified_only, limit=limit)]
        for row in rows:
            row['win_stats'] = normalize_win_stats_payload(row.get('win_stats'))
        return rows

    def export(self, out_path: Path, fmt: str = 'json', qualified_only: bool = True, limit: int = 100) -> Path:
        rows = self.latest_results(qualified_only=qualified_only, limit=limit)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if fmt == 'json':
            out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        elif fmt == 'csv':
            with out_path.open('w', encoding='utf-8', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [])
                if rows:
                    writer.writeheader()
                    writer.writerows(rows)
        else:
            raise ValueError(f'unsupported format: {fmt}')
        return out_path
