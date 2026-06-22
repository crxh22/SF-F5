"""
Demo-data seed script for the Melonticos ERP.

Usage (from erp-workspace/backend/):
    DJANGO_SETTINGS_MODULE=erp.settings.dev python seed_demo.py

Or via manage.py shell:
    DJANGO_SETTINGS_MODULE=erp.settings.dev python manage.py shell < seed_demo.py

Idempotent: uses get_or_create throughout; all demo objects are prefixed "Demo "
so they can be identified and removed. Safe to run multiple times.

NOTE on stock (section 4):
    Lot rows are written ONLY by open_lots_for_document() which reads StockMovement
    rows produced by the document engine (finalize → producer → R2 journal → lots).
    Creating a Lot directly is unsafe — it would produce orphaned FIFO cost rows
    with no matching R2 entries, breaking stock_state() and project_lots() calculations.
    Therefore this script does NOT create Lot / StockMovement rows directly.
    To get real initial stock you must post a PurchaseReception or InventoryDocument
    through the document engine (see NOTE at bottom).

NOTE on supplier order + reception (section 5):
    Both require the document engine (EngineContextRequired guard on Document.save()).
    Seeding them correctly through the engine needs the full request/user context
    that manage.py shell cannot easily replicate without a test client.
    SKIPPED — architect should create the first reception via the ERP UI.
"""

import os
import sys
import django

# ── Bootstrap Django if run as a standalone script ──────────────────────────
if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "erp.settings.dev"

# Allow running both as `python seed_demo.py` from backend/ and via shell pipe.
from django.conf import settings as _dj_settings  # noqa: E402

if not _dj_settings.configured:
    django.setup()

# ── Imports (after setup) ────────────────────────────────────────────────────
from django.db import transaction

from apps.parties.models import (
    Counterparty,
    CounterpartyRole,
    Contract,
    ContractType,
    OwnPJ,
)
from apps.nomenclature.models import (
    GenericPart,
    SpecificPart,
    Warehouse,
)


# ════════════════════════════════════════════════════════════════════════════
# Helper
# ════════════════════════════════════════════════════════════════════════════

def log(entity: str, name: str, created: bool) -> None:
    status = "CREATED" if created else "already existed"
    print(f"  [{entity}] {status}: {name}")


# ════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Counterparties + Roles
# ════════════════════════════════════════════════════════════════════════════
# Required fields on Counterparty: name (CharField, unique not enforced at DB
# but conventionally unique for commercial parties).
# IMPORTANT: do NOT create system counterparties — OwnPJ.save() auto-creates
# "TVA [pj_name]" and "Impozit pe venit [pj_name]"; we leave those alone.

CLIENTS = [
    "Demo Auto Rapid SRL",
    "Demo Munteanu & Asociații SRL",
    "Demo CarFix Iași SRL",
]

SUPPLIERS = [
    "Demo AutoParts Romania SRL",
    "Demo EuroPiese Cluj SA",
    "Demo MotoImport Brașov SRL",
]

OUTSOURCERS = [
    "Demo Vopsitorie Profesionala SRL",
]


def seed_counterparties():
    print("\n── Section 1: Counterparties & Roles ──")
    created_count = 0

    def make_party(name, roles):
        nonlocal created_count
        cp, created = Counterparty.objects.get_or_create(
            name=name,
            defaults={"system_kind": None, "system_own_pj": None},
        )
        log("Counterparty", name, created)
        if created:
            created_count += 1
        for role_code in roles:
            role_obj, role_created = CounterpartyRole.objects.get_or_create(
                counterparty=cp,
                role=role_code,
            )
            if role_created:
                log("  CounterpartyRole", f"{name} → {role_code}", True)
        return cp

    clients = [make_party(n, ["client"]) for n in CLIENTS]
    suppliers = [make_party(n, ["supplier"]) for n in SUPPLIERS]
    outsourcers = [make_party(n, ["outsourcer"]) for n in OUTSOURCERS]

    print(f"  → {len(clients)} clients, {len(suppliers)} suppliers, {len(outsourcers)} outsourcers")
    return clients, suppliers, outsourcers


# ════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Contracts
# ════════════════════════════════════════════════════════════════════════════
# Required fields on Contract:
#   counterparty (FK), contract_type (FK), delivery_payment_mode ("alb"|"negru"),
#   own_pj (FK, required when delivery_payment_mode="alb" — enforced by DB constraint
#   contract_own_pj_iff_alb; must be null when mode="negru").
# Optional FKs left null: cash_desk, bank_account, price_level,
#   payment_category_receivable, payment_category_payable.
#
# ContractType codes seeded by apps/parties/seeds.py:
#   service, employee_contract, vanzare_in_rate, vat_acquisition, outsource
#
# OwnPJ seeded by apps/parties/seeds.py: code="melonticos_srl"

def seed_contracts(clients, suppliers, outsourcers):
    print("\n── Section 2: Contracts ──")

    # Resolve the seeded OwnPJ (must already exist from seed_launch_config)
    try:
        own_pj = OwnPJ.objects.get(code="melonticos_srl")
    except OwnPJ.DoesNotExist:
        print("  WARNING: OwnPJ 'melonticos_srl' not found — run seed_launch_config first.")
        print("  SKIPPING contract creation.")
        return []

    # Resolve ContractType rows (seeded by apps/parties/seeds.py)
    ct_service = ContractType.objects.filter(code="service").first()
    ct_outsource = ContractType.objects.filter(code="outsource").first()
    ct_vat = ContractType.objects.filter(code="vat_acquisition").first()

    if not ct_service or not ct_outsource or not ct_vat:
        print("  WARNING: ContractType rows not found — run seed_launch_config first.")
        print("  SKIPPING contract creation.")
        return []

    contracts_spec = [
        # (counterparty, contract_type, fiscal_mode, description_for_log)
        (clients[0],      ct_service,   "alb", f"service/alb for {clients[0].name}"),
        (clients[1],      ct_service,   "alb", f"service/alb for {clients[1].name}"),
        (clients[2],      ct_service,   "alb", f"service/alb for {clients[2].name}"),
        (suppliers[0],    ct_vat,       "alb", f"vat_acquisition/alb for {suppliers[0].name}"),
        (suppliers[1],    ct_vat,       "alb", f"vat_acquisition/alb for {suppliers[1].name}"),
        (outsourcers[0],  ct_outsource, "alb", f"outsource/alb for {outsourcers[0].name}"),
    ]

    results = []
    for cp, ct, mode, label in contracts_spec:
        # Unique key for idempotency: (counterparty, contract_type, delivery_payment_mode)
        # The model has no unique constraint on these, so we use get_or_create with
        # a reasonable lookup key. If called twice it finds the existing row.
        defaults = {
            "delivery_payment_mode": mode,
            "own_pj": own_pj if mode == "alb" else None,
        }
        contract, created = Contract.objects.get_or_create(
            counterparty=cp,
            contract_type=ct,
            delivery_payment_mode=mode,
            defaults=defaults,
        )
        log("Contract", label, created)
        results.append(contract)

    print(f"  → {len(results)} contracts")
    return results


# ════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Parts (GenericPart → SpecificPart) + Warehouse
# ════════════════════════════════════════════════════════════════════════════
# GenericPart required fields: name (from NomenclatureBase). No extra fields.
# SpecificPart required fields: name (NomenclatureBase), generic_part (FK, null/blank
#   allowed). original_code must be unique where set (partial constraint).
#   aftermarket_codes defaults to [].
#
# 10 parts covering common auto-body repair shop inventory (Romanian names).

PARTS_DATA = [
    # (generic_name, specific_name, original_code, manufacturer)
    ("Demo Bara față",         "Demo Bara față Toyota Corolla 2018",   "DEMO-TYT-BF-2018", "Toyota"),
    ("Demo Bara spate",        "Demo Bara spate Dacia Logan 2020",     "DEMO-DAC-BS-2020", "Dacia"),
    ("Demo Capotă motor",      "Demo Capotă motor Volkswagen Golf 7",  "DEMO-VW-CM-G7",    "Volkswagen"),
    ("Demo Aripă față stângă", "Demo Aripă față stângă Ford Focus 3",  "DEMO-FRD-AF-F3",   "Ford"),
    ("Demo Aripă față dreaptă","Demo Aripă față dreaptă Renault Megane","DEMO-RNL-AD-MG",  "Renault"),
    ("Demo Far stâng",         "Demo Far stâng Skoda Octavia 3",       "DEMO-SKD-FS-O3",   "Skoda"),
    ("Demo Far drept",         "Demo Far drept BMW Seria 3 F30",       "DEMO-BMW-FD-F30",  "BMW"),
    ("Demo Parbriz",           "Demo Parbriz Opel Astra J",            "DEMO-OPL-PB-AJ",   "Opel"),
    ("Demo Ușă față stângă",   "Demo Ușă față stângă Mercedes C-Class","DEMO-MRC-US-C",    "Mercedes"),
    ("Demo Oglindă laterală",  "Demo Oglindă laterală universală",     "DEMO-UNV-OG-01",   "Universal"),
]

WAREHOUSE_CODE = "demo_depozit_central"
WAREHOUSE_NAME = "Demo Depozit Central"


def seed_parts_and_warehouse():
    print("\n── Section 3: GenericParts, SpecificParts, Warehouse ──")

    # Warehouse
    warehouse, w_created = Warehouse.objects.get_or_create(
        code=WAREHOUSE_CODE,
        defaults={"name": WAREHOUSE_NAME},
    )
    log("Warehouse", WAREHOUSE_NAME, w_created)

    specific_parts = []
    for gen_name, spec_name, orig_code, manufacturer in PARTS_DATA:
        # GenericPart — keyed by name
        gp, gp_created = GenericPart.objects.get_or_create(
            name=gen_name,
        )
        log("GenericPart", gen_name, gp_created)

        # SpecificPart — keyed by original_code (unique where set)
        sp, sp_created = SpecificPart.objects.get_or_create(
            original_code=orig_code,
            defaults={
                "name": spec_name,
                "generic_part": gp,
                "manufacturer": manufacturer,
                "aftermarket_codes": [],
            },
        )
        log("SpecificPart", spec_name, sp_created)
        specific_parts.append(sp)

    print(f"  → 1 warehouse, {len(specific_parts)} specific parts (10 generic parents)")
    return warehouse, specific_parts


# ════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Stock (SKIPPED — see module docstring)
# ════════════════════════════════════════════════════════════════════════════

def note_stock_skipped():
    print("\n── Section 4: Initial Stock ── SKIPPED")
    print("  Lot rows are written exclusively by open_lots_for_document() which")
    print("  requires StockMovement rows in R2 produced by the document engine.")
    print("  Creating Lot rows directly would corrupt FIFO cost accounting.")
    print("  → To seed stock: create a PurchaseReception via the ERP UI or API.")


# ════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Supplier order + reception (SKIPPED — see module docstring)
# ════════════════════════════════════════════════════════════════════════════

def note_procurement_skipped():
    print("\n── Section 5: Supplier Order + Reception ── SKIPPED")
    print("  Document.save() is guarded by EngineContextRequired.")
    print("  These documents must be created through the document engine (UI/API).")
    print("  → Use the ERP UI: Achiziții → Comandă furnizor → Recepție.")


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("Demo data seed — Melonticos ERP")
    print("=" * 60)

    with transaction.atomic():
        clients, suppliers, outsourcers = seed_counterparties()
        seed_contracts(clients, suppliers, outsourcers)
        seed_parts_and_warehouse()

    note_stock_skipped()
    note_procurement_skipped()

    print("\n" + "=" * 60)
    print("Seed complete. All sections wrapped in a single transaction.")
    print("=" * 60)


if __name__ == "__main__":
    main()
