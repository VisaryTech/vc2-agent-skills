"""Normalized primary-document import; append-only remote operations."""
from decimal import Decimal, InvalidOperation
from .common import Error, date, decimal, digest, fields, guid, require, rounded, tax_id

TOP = {"operation_id", "type", "number", "date", "organization", "supplier", "contract", "currency",
       "lines", "total", "vat_total", "includes_vat", "function", "invoice", "basis"}


def validate_input(data, rates):
    fields(data, TOP, {"operation_id", "type", "number", "date", "organization", "supplier", "total", "vat_total"})
    guid(data["operation_id"])
    require(data["type"] in {"upd", "torg12", "act", "invoice"}, "Неизвестный тип первички.")
    require(isinstance(data["number"], str) and bool(data["number"].strip()), "Укажите номер первички.")
    date(data["date"])
    tax_id(data["supplier"])
    total, vat_total = decimal(data["total"], True), decimal(data["vat_total"], True)
    require(vat_total <= total, "НДС превышает сумму документа.")
    if data["type"] == "invoice":
        require(not set(data) & {"lines", "includes_vat", "function", "invoice"}, "Отдельный счёт-фактура не создаёт поступление.")
        basis = data.get("basis")
        fields(basis, {"id", "number", "date"})
        if "id" in basis:
            guid(basis["id"])
            require(set(basis) == {"id"}, "Для основания укажите ID либо номер и дату.")
        else:
            require(set(basis) == {"number", "date"} and isinstance(basis["number"], str) and basis["number"], "Укажите номер и дату основания.")
            date(basis["date"])
        return
    require("basis" not in data, "Основание задаётся только для отдельного счёта-фактуры.")
    require(type(data.get("includes_vat")) is bool, "Укажите includes_vat: boolean.")
    if data["type"] == "upd":
        require(data.get("function") in {"transfer", "invoice_transfer"}, "Укажите функцию УПД: transfer или invoice_transfer.")
        require(("invoice" in data) == (data["function"] == "invoice_transfer"), "Реквизиты счёта-фактуры должны соответствовать функции УПД.")
    else:
        require("function" not in data, "function применим только к УПД.")
    if "invoice" in data:
        fields(data["invoice"], {"number", "date"}, {"number", "date"})
        require(isinstance(data["invoice"]["number"], str) and data["invoice"]["number"].strip(), "Укажите номер счёта-фактуры.")
        date(data["invoice"]["date"])
    rows = data.get("lines")
    require(isinstance(rows, list) and 0 < len(rows) <= 10000, "Нужно от 1 до 10000 строк.")
    sums = {k: Decimal(0) for k in ("net", "vat", "gross")}
    for row in rows:
        fields(row, {"kind", "item", "unit", "quantity", "price", "net", "vat", "gross", "vat_rate"},
               {"kind", "item", "unit", "quantity", "price", "net", "vat", "gross", "vat_rate"})
        require(row["kind"] in {"goods", "services"}, "Неверный вид строки.")
        require(data["type"] != "act" or row["kind"] == "services", "Акт должен содержать услуги.")
        require(data["type"] != "torg12" or row["kind"] == "goods", "ТОРГ-12 должна содержать товары.")
        rate = row["vat_rate"]
        require(isinstance(rate, str) and rate in rates, "Ставка НДС не сопоставлена в профиле.")
        quantity, price = decimal(row["quantity"]), decimal(row["price"])
        require(quantity > 0, "Количество должно быть положительным.")
        values = {k: decimal(row[k], True) for k in sums}
        require(values["net"] + values["vat"] == values["gross"], "Нетто + НДС не равно брутто строки.", "amount_mismatch")
        priced = values["gross"] if data["includes_vat"] else values["net"]
        require(rounded(quantity * price) == priced, "Количество × цена не соответствует сумме строки.", "amount_mismatch")
        percent = Decimal(0 if rate == "none" else rate)
        expected_vat = rounded(values["gross"] * percent / (100 + percent)) if data["includes_vat"] else rounded(values["net"] * percent / 100)
        require(values["vat"] == expected_vat, "НДС строки не соответствует ставке и режиму цены.", "amount_mismatch")
        for key in sums:
            sums[key] += values[key]
    require(sums["gross"] == total and sums["vat"] == vat_total, "Итоги документа не совпадают со строками.", "amount_mismatch")


class Importer:
    def __init__(self, client, state):
        self.client, self.state = client, state
        self.journal = None

    def checked_reference(self, role, row):
        mapping = self.client.role(role)["fields"]
        if "deletion_mark" in mapping:
            require(row.get(mapping["deletion_mark"]) is False, "Ссылка помечена на удаление: " + role, "reference_conflict")
        return row

    def resolve(self, role, selector):
        spec = self.client.role(role)
        fields(selector, {"id", "code", "inn", "kpp"})
        if "id" in selector:
            require(set(selector) == {"id"}, "Укажите только ID либо точный код/ИНН.")
            return self.checked_reference(role, self.client.read(role, guid(selector["id"])))
        if set(selector) == {"code"}:
            require(isinstance(selector["code"], str) and selector["code"], "Код должен быть непустой строкой.")
        else:
            require(role == "organizations" and set(selector) <= {"inn", "kpp"} and "inn" in selector,
                    "Для выбора нужен ID, код либо ИНН/КПП организации.")
            inn, kpp = tax_id(selector)
            selector = {"inn": inn, "kpp": kpp}
        matches = self.client.query(role, selector)
        require(len(matches) == 1, "Нужно единственное точное соответствие: " + role, "missing_reference" if not matches else "ambiguous_reference")
        require(bool(matches[0].get(spec["fields"]["id"])), "У справочника нет ID.", "invalid_response")
        return self.checked_reference(role, matches[0])

    def identity(self, role, row):
        return guid(row[self.client.role(role)["fields"]["id"]])

    def supplier_payload(self, data):
        inn, kpp = tax_id(data)
        require(isinstance(data.get("name"), str) and data["name"].strip(), "Для нового контрагента нужно name.", "missing_data")
        logical = {"inn": inn, "kpp": kpp, "name": data["name"]}
        if "deletion_mark" in self.client.role("counterparties")["fields"]:
            logical["deletion_mark"] = False
        if "kind" in data:
            require("kind" in self.client.role("counterparties")["fields"], "kind не сопоставлен в профиле.", "invalid_profile")
            kinds = self.client.profile.get("counterparty_kinds", {})
            require(isinstance(data["kind"], str) and data["kind"] in kinds, "Неизвестный вид контрагента.")
            logical["kind"] = kinds[data["kind"]]
        return self.client.payload("counterparties", logical)

    def supplier(self, data):
        inn, kpp = tax_id(data)
        matches = self.client.query("counterparties", {"inn": inn, "kpp": kpp})
        require(len(matches) <= 1, "Несколько контрагентов с одинаковыми ИНН/КПП.", "ambiguous_reference")
        if matches:
            return self.checked_reference("counterparties", matches[0]), None
        return None, self.supplier_payload(data)

    def compare(self, role, actual, expected):
        entity = self.client.schema.entity(self.client.role(role)["entity_set"])
        def subset(spec, actual_value, expected_value):
            require(isinstance(actual_value, dict), "Неверный объект проверки.", "duplicate_conflict")
            for key, value in expected_value.items():
                require(key in actual_value, "Не хватает реквизита при сверке.", "duplicate_conflict")
                observed = actual_value[key]
                prop = spec["properties"][key]
                kind = prop["Type"]
                if value is None:
                    equal = observed is None or (kind == "Edm.Guid" and observed == "00000000-0000-0000-0000-000000000000")
                elif kind in {"Edm.Decimal", "Edm.Double"} or kind.startswith("Edm.Int"):
                    try:
                        equal = Decimal(str(observed)) == Decimal(str(value))
                    except Exception:
                        equal = False
                elif kind in {"Edm.DateTime", "Edm.DateTimeOffset"}:
                    # OData v3 may serialize dates as /Date(milliseconds[+offset])/.
                    if isinstance(observed, str) and observed.startswith("/Date("):
                        import datetime as dt
                        import re
                        match = re.fullmatch(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/", observed)
                        require(bool(match), "Неизвестный формат даты сервера.", "invalid_response")
                        observed = dt.datetime.fromtimestamp(int(match[1]) / 1000, dt.timezone.utc).date().isoformat()
                    equal = isinstance(observed, str) and observed[:10] == value[:10]
                elif kind.startswith("Collection("):
                    require(isinstance(observed, list) and len(observed) == len(value), "Различается состав строк.", "duplicate_conflict")
                    row_spec = self.client.schema.type(kind[11:-1])
                    for old, new in zip(observed, value):
                        subset(row_spec, old, new)
                    equal = True
                elif kind == "Edm.Guid":
                    equal = isinstance(observed, str) and observed.lower() == value.lower()
                else:
                    equal = type(observed) is type(value) and observed == value
                require(equal, "Реквизиты или суммы существующего объекта отличаются.", "duplicate_conflict")
        subset(entity, actual, expected)

    def duplicate(self, role, identity, expected, allow_posted=True):
        rows = self.client.query(role, identity)
        require(len(rows) <= 1, "Найдено несколько документов с одинаковой первичкой.", "duplicate_conflict")
        if not rows:
            return None
        row = self.client.read(role, self.identity(role, rows[0]))
        compare_body = dict(expected)
        # Already existing documents may be posted. Never change their state.
        if allow_posted and role != "counterparties":
            compare_body.pop(self.client.role(role)["fields"]["posted"], None)
        self.compare(role, row, compare_body)
        return self.identity(role, row)

    def persist(self):
        self.state.save(self.state.path(self.journal["operation_id"]), self.journal)

    def ensure(self, stage, role, identity, body):
        previous = self.journal["stages"].get(stage)
        if previous and previous.get("id"):
            # Own creations must remain unposted. Preexisting duplicates may be posted.
            actual = self.client.read(role, previous["id"])
            expected = dict(body)
            if previous.get("status") == "existing" and role != "counterparties":
                expected.pop(self.client.role(role)["fields"]["posted"], None)
            self.compare(role, actual, expected)
            if previous["status"] == "created_unverified":
                previous["status"] = "verified"
                self.persist()
            return previous["id"]
        existing = self.duplicate(role, identity, body, allow_posted=not previous)
        if existing:
            self.journal["stages"][stage] = {"status": "verified" if previous else "existing", "id": existing}
            self.persist()
            return existing
        require(not previous or previous.get("status") != "sending", "Результат прежнего POST не установлен. Повторное создание заблокировано.", "write_outcome_unknown")
        self.journal["stages"][stage] = {"status": "sending"}
        self.persist()
        self.state.log(event="create_start", operation_id=self.journal["operation_id"], role=role)
        try:
            result = self.client.create(role, body)
        except Error as exc:
            if exc.code != "write_outcome_unknown":
                self.journal["stages"][stage] = {"status": "rejected"}
                self.persist()
            else:
                # One read reconciliation is safe. Absence does not authorize resending.
                try:
                    existing = self.duplicate(role, identity, body, allow_posted=False)
                except Error:
                    existing = None
                if existing:
                    self.journal["stages"][stage] = {"status": "verified", "id": existing}
                    self.persist()
                    return existing
            raise
        if isinstance(result, dict) and "d" in result:
            result = result["d"]
        key = self.client.role(role)["fields"]["id"]
        try:
            identity_value = guid(result[key]) if isinstance(result, dict) and key in result else None
        except Error:
            identity_value = None
        if identity_value:
            # Store the returned key before the verification GET can fail.
            self.journal["stages"][stage] = {"status": "created_unverified", "id": identity_value}
            self.persist()
            self.compare(role, self.client.read(role, identity_value), body)
        else:
            identity_value = self.duplicate(role, identity, body, allow_posted=False)
            require(identity_value is not None, "POST принят, но созданный объект не найден.", "write_outcome_unknown")
        self.journal["stages"][stage] = {"status": "verified", "id": identity_value}
        self.persist()
        self.state.log(event="create_verified", operation_id=self.journal["operation_id"], role=role, id=identity_value)
        return identity_value

    def receipt_body(self, data, org_id, supplier_id, contract_id, currency_id):
        spec = self.client.role("receipts")
        logical = {"organization": org_id, "number": data["number"], "date": data["date"],
                   "total": data["total"], "posted": False, "includes_vat": data["includes_vat"]}
        if supplier_id:
            logical["supplier"] = supplier_id
        for name, value in {"contract": contract_id, "currency": currency_id, "vat_total": data["vat_total"], "deletion_mark": False, "document_date": data["date"], "is_upd": data["type"] == "upd"}.items():
            if name in spec["fields"]:
                logical[name] = value
        if "document_kind" in spec["fields"]:
            kinds = self.client.profile.get("receipt_kinds", {})
            require(data["type"] in kinds, "Вид поступления не сопоставлен.", "invalid_profile")
            value = kinds[data["type"]]
            if isinstance(value, dict):
                composition = {row["kind"] for row in data["lines"]}
                mode = "mixed" if len(composition) > 1 else next(iter(composition))
                require(mode in value, "Такой состав строк не сопоставлен виду операции.", "invalid_profile")
                value = value[mode]
            logical["document_kind"] = value
        raw = {**spec.get("defaults", {}), **{spec["fields"][k]: v for k, v in logical.items()}}
        tables = {}
        counts = {}
        for row in data["lines"]:
            table = spec["tables"][row["kind"]]
            actual = table["property"]
            tables.setdefault(actual, [])
            counts[actual] = counts.get(actual, 0) + 1
            item_row = self.resolve("items", row["item"])
            item = self.identity("items", item_row)
            item_fields = self.client.role("items")["fields"]
            require("is_folder" not in item_fields or item_row.get(item_fields["is_folder"]) is False, "Номенклатура является группой.", "reference_conflict")
            require("is_service" not in item_fields or item_row.get(item_fields["is_service"]) is (row["kind"] == "services"), "Вид номенклатуры не соответствует строке.", "reference_conflict")
            unit = self.identity("units", self.resolve("units", row["unit"]))
            if "unit" in item_fields:
                require(item_row.get(item_fields["unit"]) == unit, "Единица не совпадает с единицей номенклатуры.", "reference_conflict")
            amount_mode = table["amount_mode"]
            mode = ("gross" if data["includes_vat"] else "net") if amount_mode == "input" else amount_mode
            values = {"item": item, "unit": unit, "quantity": row["quantity"], "price": row["price"],
                      "amount": row[mode], "vat": row["vat"], "vat_rate": self.client.profile["vat_rates"][row["vat_rate"]]}
            if "line_no" in table["fields"]:
                values["line_no"] = counts[actual]
            tables[actual].append({**table.get("defaults", {}), **{table["fields"][k]: v for k, v in values.items() if k in table["fields"]}})
        # Include all mapped tables, so a duplicate cannot conceal extra service/goods rows.
        for table in spec["tables"].values():
            raw[table["property"]] = tables.get(table["property"], [])
        return self.client.encode(self.client.schema.entity(spec["entity_set"]), raw)

    def invoice_body(self, data, invoice, org_id, supplier_id, basis_id, contract_id, currency_id):
        spec = self.client.role("invoices")
        logical = {"organization": org_id, "number": invoice["number"], "date": invoice["date"], "posted": False}
        if supplier_id:
            logical["supplier"] = supplier_id
        if basis_id and "basis" in spec["fields"]:
            logical["basis"] = basis_id
        for name, value in {"contract": contract_id, "currency": currency_id, "total": data["total"], "vat_total": data["vat_total"], "deletion_mark": False, "document_date": invoice["date"]}.items():
            if name in spec["fields"]:
                logical[name] = value
        body = self.client.payload("invoices", logical)
        if basis_id:
            self.set_basis(body, basis_id)
        return body

    def set_basis(self, body, basis_id):
        spec = self.client.role("invoices")
        if "basis" in spec["fields"]:
            body[spec["fields"]["basis"]] = guid(basis_id)
        else:
            table = spec["basis_table"]
            values = {"basis": guid(basis_id)}
            if "line_no" in table["fields"]:
                values["line_no"] = 1
            raw = {**table.get("defaults", {}), **{table["fields"][key]: value for key, value in values.items()}}
            entity = self.client.schema.entity(spec["entity_set"])
            body[table["property"]] = self.client.value(entity["properties"][table["property"]], [raw])

    def find_basis(self, data, org_id, supplier_id):
        require(supplier_id is not None, "Контрагент основания не найден.", "missing_reference")
        basis = data["basis"]
        expected = self.client.payload("receipts", {"organization": org_id, "supplier": supplier_id, "total": data["total"]})
        if "vat_total" in self.client.role("receipts")["fields"]:
            expected.update(self.client.payload("receipts", {"vat_total": data["vat_total"]}))
        if "id" in basis:
            row = self.client.read("receipts", basis["id"])
        else:
            rows = self.client.query("receipts", {"organization": org_id, "supplier": supplier_id, **basis})
            require(len(rows) == 1, "Основание не найдено однозначно; укажите basis.id.", "missing_reference" if not rows else "ambiguous_reference")
            row = self.client.read("receipts", self.identity("receipts", rows[0]))
        self.compare("receipts", row, expected)
        receipt_spec = self.client.role("receipts")
        if "deletion_mark" in receipt_spec["fields"]:
            require(row.get(receipt_spec["fields"]["deletion_mark"]) is False, "Основание помечено на удаление.", "duplicate_conflict")
        if "vat_total" not in receipt_spec["fields"]:
            vat = Decimal(0)
            seen = set()
            for table in receipt_spec["tables"].values():
                if table["property"] in seen:
                    continue
                seen.add(table["property"])
                rows = row.get(table["property"])
                require(isinstance(rows, list), "Нельзя проверить НДС основания.", "invalid_response")
                for line in rows:
                    try:
                        value = Decimal(str(line[table["fields"]["vat"]]))
                        require(value.is_finite(), "Некорректный НДС основания.", "invalid_response")
                        vat += value
                    except (KeyError, ValueError, InvalidOperation):
                        raise Error("invalid_response", "Нельзя проверить НДС основания.") from None
            require(vat == decimal(data["vat_total"], True), "НДС основания не совпадает.", "amount_mismatch")
        return self.identity("receipts", row)

    def execute(self, data, dry_run=False):
        validate_input(data, self.client.profile["vat_rates"])
        data = {**data, "operation_id": guid(data["operation_id"])}
        if dry_run:
            return self.run(data, True)
        with self.state.lock():
            path = self.state.path(data["operation_id"])
            self.journal = self.state.read(data["operation_id"]) if path.exists() else {
                "operation_id": data["operation_id"], "input_sha256": digest(data), "stages": {}, "status": "pending"}
            require(self.journal["input_sha256"] == digest(data), "operation_id уже использован с другим входом.", "operation_conflict")
            self.persist()
            try:
                result = self.run(data, False)
            except Error as exc:
                self.journal["status"] = "incomplete"
                self.journal["error"] = exc.code
                self.persist()
                self.state.log(event="import_error", operation_id=data["operation_id"], error=exc.code)
                raise
            self.journal["status"] = "complete"
            self.journal.pop("error", None)
            self.persist()
            return result

    def run(self, data, dry_run):
        org_id = self.identity("organizations", self.resolve("organizations", data["organization"]))
        supplier, supplier_body = self.supplier(data["supplier"])
        supplier_id = self.identity("counterparties", supplier) if supplier else None
        contract_id, currency_id = None, None
        if data.get("contract") is not None:
            require("contract" in self.client.role("receipts")["fields"], "Договор поступления не сопоставлен в профиле.", "invalid_profile")
            contract = self.resolve("contracts", data["contract"])
            mapping = self.client.role("contracts")["fields"]
            require(contract.get(mapping["organization"]) == org_id and supplier_id is not None
                    and contract.get(mapping["supplier"]) == supplier_id, "Договор не соответствует организации/контрагенту.", "reference_conflict")
            contract_id = self.identity("contracts", contract)
        if data.get("currency") is not None:
            require("currency" in self.client.role("receipts")["fields"], "Валюта поступления не сопоставлена в профиле.", "invalid_profile")
            currency_id = self.identity("currencies", self.resolve("currencies", data["currency"]))
        receipt, basis_id = None, None
        if data["type"] == "invoice":
            basis_id = self.find_basis(data, org_id, supplier_id)
            extra = {name: value for name, value in {"contract": contract_id, "currency": currency_id}.items() if value is not None}
            if extra:
                self.compare("receipts", self.client.read("receipts", basis_id), self.client.payload("receipts", extra))
            invoice = {"number": data["number"], "date": data["date"]}
        else:
            invoice = data.get("invoice")
            receipt = self.receipt_body(data, org_id, supplier_id, contract_id, currency_id)
        invoice_body = self.invoice_body(data, invoice, org_id, supplier_id, basis_id, contract_id, currency_id) if invoice else None
        if supplier_body is not None:
            self.client.check_required("counterparties", supplier_body)
        if receipt is not None:
            deferred = {self.client.role("receipts")["fields"]["supplier"]} if supplier_id is None else set()
            self.client.check_required("receipts", receipt, deferred)
        if invoice_body is not None:
            spec = self.client.role("invoices")
            deferred = {spec["fields"]["supplier"]} if supplier_id is None else set()
            if basis_id is None:
                deferred.add(spec["fields"].get("basis") or spec["basis_table"]["property"])
            self.client.check_required("invoices", invoice_body, deferred)
        # Preflight existing primary documents and invoices before creating anything.
        receipt_identity = {"organization": org_id, "supplier": supplier_id, "number": data["number"], "date": data["date"]}
        if supplier_id and receipt is not None:
            basis_id = self.duplicate("receipts", receipt_identity, receipt)
        invoice_identity = {"organization": org_id, "supplier": supplier_id, **invoice} if invoice else None
        existing_invoice = None
        if supplier_id and invoice_body is not None:
            if basis_id:
                self.set_basis(invoice_body, basis_id)
            # Search without basis to catch a conflicting invoice attached elsewhere.
            existing_invoice = self.duplicate("invoices", invoice_identity, invoice_body)
            if existing_invoice and basis_id is None:
                raise Error("duplicate_conflict", "Счёт-фактура уже есть, но поступление-основание не найдено.")
        if dry_run:
            return {"ok": True, "dry_run": True, "pending_supplier": supplier_id is None,
                    "counterparty_create": supplier_body, "receipt": receipt, "invoice": invoice_body,
                    "existing_receipt_id": basis_id, "existing_invoice_id": existing_invoice,
                    "note": "Неизвестные supplier/basis ID будут подставлены после создания; запись не выполнялась."}
        if supplier_id is None:
            supplier_id = self.ensure("counterparty", "counterparties", {"inn": data["supplier"]["inn"], "kpp": data["supplier"].get("kpp", "")}, supplier_body)
        elif "counterparty" in self.journal["stages"]:
            supplier_id = self.ensure("counterparty", "counterparties", {"inn": data["supplier"]["inn"], "kpp": data["supplier"].get("kpp", "")}, self.supplier_payload(data["supplier"]))
        if receipt is not None:
            receipt[self.client.role("receipts")["fields"]["supplier"]] = supplier_id
            receipt_identity["supplier"] = supplier_id
            basis_id = self.ensure("receipt", "receipts", receipt_identity, receipt)
        invoice_id = None
        if invoice_body is not None:
            invoice_body[self.client.role("invoices")["fields"]["supplier"]] = supplier_id
            self.set_basis(invoice_body, basis_id)
            invoice_identity["supplier"] = supplier_id
            invoice_id = self.ensure("invoice", "invoices", invoice_identity, invoice_body)
        return {"ok": True, "operation_id": data["operation_id"], "supplier_id": supplier_id,
                "receipt_id": basis_id, "invoice_id": invoice_id, "stages": self.journal["stages"]}
