"""Metadata-driven collection queries and allowlisted register functions."""
import re
import urllib.parse as url

from .common import require
from .filters import Predicate


def unwrap(result):
    return result["d"] if isinstance(result, dict) and "d" in result else result


class ReadQueries:
    def query_options(self, spec, where, select, order_by, tree, presentations, expand):
        predicate = Predicate(self, spec, tree, where)
        params = {"$format": "json"}
        for name in select:
            if name == "*":
                continue
            if isinstance(name, str) and name.endswith("____Presentation"):
                base = name[:-len("____Presentation")]
                require(base in spec["properties"] or base + "_Key" in spec["properties"]
                        or base in spec["navigation"], "Неизвестное поле представления.")
            else:
                self.schema.property(spec, name)
        for name in expand:
            self.schema.navigation(spec, name)
        if expand:
            params["$expand"] = ",".join(dict.fromkeys(expand))
        if select or presentations:
            fields = list(select) if select else ["*"]
            if "*" not in fields:
                fields.extend(sorted(predicate.fields))
                fields.extend(expand)
            if presentations:
                fields.append("*____Presentation")
            params["$select"] = ",".join(dict.fromkeys(fields))
        ordering = []
        for term in order_by:
            pieces = term.split()
            require(len(pieces) in {1, 2} and (len(pieces) == 1 or pieces[1] in {"asc", "desc"}),
                    "Некорректная сортировка.")
            self.schema.property(spec, pieces[0])
            ordering.append(" ".join(pieces))
        if ordering:
            params["$orderby"] = ",".join(ordering)
        if predicate.text:
            params["$filter"] = predicate.text
        return params, predicate

    def collection(self, path, spec, params, predicate, select=(), presentations=False,
                   expand=(), limit=100, pagination="next-link", page_size=100):
        require(limit is None or type(limit) is int and 1 <= limit <= 10000, "Лимит должен быть 1–10000.")
        require(pagination in {"next-link", "skip"}, "Неизвестный режим пагинации.")
        require(type(page_size) is int and 1 <= page_size <= 10000, "Размер страницы должен быть 1–10000.")
        params = dict(params)
        if pagination == "skip":
            require(limit is None, "Пагинация skip требует --all.")
            require(bool(spec["keys"]), "Для skip нужны ключи metadata.", "operation_not_supported")
            ordering = params.get("$orderby", "").split(",") if params.get("$orderby") else []
            ordered = {term.split()[0] for term in ordering}
            ordering.extend(name + " asc" for name in spec["keys"] if name not in ordered)
            params.update({"$orderby": ",".join(ordering), "$top": str(page_size), "$skip": "0"})
        elif limit is not None:
            params["$top"] = str(limit)
        def address():
            return path + "?" + url.urlencode(params, quote_via=url.quote)
        current = address()
        rows, visited = [], set()
        while current:
            target = self.transport.safe_url(current)
            require(target not in visited and len(visited) < 10000,
                    "Циклическая или слишком длинная пагинация.", "invalid_response")
            visited.add(target)
            result = unwrap(self.transport.request("GET", target))
            if isinstance(result, list):
                page, following = result, None
            else:
                require(isinstance(result, dict), "Некорректный ответ коллекции.", "invalid_response")
                page = result.get("value", result.get("results"))
                following = result.get("odata.nextLink", result.get("@odata.nextLink", result.get("__next")))
            require(isinstance(page, list), "Некорректный ответ коллекции.", "invalid_response")
            require(all(predicate.matches(row) for row in page), "Ответ не соответствует фильтру.", "invalid_response")
            require(following is None or isinstance(following, str), "Некорректный nextLink.", "invalid_response")
            if following:
                self.transport.safe_url(url.urljoin(target, following))
            if select and "*" not in select:
                rows.extend({k: v for k, v in row.items() if k in select or k in expand
                             or presentations and k.endswith("____Presentation")} for row in page)
            else:
                rows.extend(page)
            require(len(rows) <= 100000, "Уточните фильтр: слишком много записей.", "query_too_large")
            if limit is not None and len(rows) >= limit:
                return rows[:limit]
            if pagination == "skip":
                require(len(page) <= page_size, "Сервер не соблюдает размер страницы.", "invalid_response")
                # A server-driven short page may still have a continuation. Do not truncate it silently.
                require(not following, "Сервер использует nextLink; выберите --pagination next-link.", "operation_not_supported")
                params["$skip"] = str(int(params["$skip"]) + len(page))
                current = address() if page else None
            else:
                current = url.urljoin(target, following) if following else None
        return rows

    def query_entity(self, entity, where=None, select=(), order_by=(), limit=100, *,
                     filter_tree=None, presentations=False, expand=(), pagination="next-link", page_size=100):
        spec = self.schema.entity(entity)
        params, predicate = self.query_options(spec, where, select, order_by, filter_tree, presentations, expand)
        return self.collection(self.address(entity), spec, params, predicate, select, presentations,
                               expand, limit, pagination, page_size)

    def count(self, entity, where=None, filter_tree=None):
        spec = self.schema.entity(entity)
        params, _ = self.query_options(spec, where, (), (), filter_tree, False, ())
        params.update({"$top": "0", "$inlinecount": "allpages"})
        result = unwrap(self.transport.request("GET", self.address(entity) + "?" + url.urlencode(params, quote_via=url.quote)))
        require(isinstance(result, dict), "Некорректный ответ счётчика.", "invalid_response")
        value = next((result[key] for key in ("odata.count", "@odata.count", "__count") if key in result), None)
        require(value is not None, "Сервер не вернул счётчик.", "operation_not_supported")
        require(type(value) is int and 0 <= value <= 2**63-1
                or isinstance(value, str) and bool(re.fullmatch(r"[0-9]{1,19}", value)) and int(value) <= 2**63-1,
                "Некорректный счётчик.", "invalid_response")
        return int(value)

    def register(self, entity, table, *, period=None, start_period=None, end_period=None,
                 condition=None, select=(), limit=100):
        spec = self.schema.entity(entity)
        tables = {"slice-last": ("InformationRegister_", "SliceLast"),
                  "slice-first": ("InformationRegister_", "SliceFirst"),
                  "balance": ("AccumulationRegister_", "Balance"),
                  "turnovers": ("AccumulationRegister_", "Turnovers"),
                  "balance-and-turnovers": ("AccumulationRegister_", "BalanceAndTurnovers")}
        require(table in tables, "Неизвестная виртуальная таблица.")
        prefix, method = tables[table]
        require(entity.startswith(prefix), "Таблица не соответствует виду регистра.", "operation_not_supported")
        require(not (period is not None and table in {"turnovers", "balance-and-turnovers"})
                and not (table not in {"turnovers", "balance-and-turnovers"}
                         and (start_period is not None or end_period is not None)), "Неверные параметры периода.")
        operation, result_spec = self.schema.register_function(entity, method)
        declarations = operation["parameters"][1:]
        require(all(isinstance(p.get("Name"), str) and p["Name"]
                    and isinstance(p.get("Type"), str) and p["Type"] for p in declarations),
                "Неполная схема параметров функции.", "operation_not_supported")
        require(len({p.get("Name") for p in declarations}) == len(declarations),
                "Неоднозначные параметры функции.", "operation_not_supported")
        values = {"Period": period, "StartPeriod": start_period, "EndPeriod": end_period}
        if condition is not None:
            values["Condition"] = Predicate(self, spec, condition).text
        supplied = {name: value for name, value in values.items() if value is not None}
        known = {p.get("Name"): p for p in declarations}
        require(supplied.keys() <= known.keys(), "Параметр не объявлен в metadata.", "operation_not_supported")
        for param in declarations:
            require(param.get("Mode", "In") == "In", "Неподдерживаемое направление параметра.", "operation_not_supported")
            require(param.get("Nullable", "true").lower() != "false" or param["Name"] in supplied,
                    "Не задан обязательный параметр функции.")
        if "Condition" in supplied:
            require(known["Condition"].get("Type") == "Edm.String", "Condition должен иметь тип String.", "operation_not_supported")
        args = [name + "=" + self.literal(known[name], supplied[name]) for name in known if name in supplied]
        path = self.address(entity) + "/" + method + "(" + url.quote(",".join(args), safe="=,'()") + ")"
        params, predicate = self.query_options(result_spec, None, select, (), None, False, ())
        return self.collection(path, result_spec, params, predicate, select=select, limit=limit)
