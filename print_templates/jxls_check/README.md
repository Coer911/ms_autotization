# Проверка шаблона движком jXLS

МойСклад обрабатывает шаблоны в jXLS на Apache POI и на ошибку отвечает только
«Некорректный шаблон печатной формы». Здесь тот же движок (jXLS 1.0.6) и поддельный
документ (`src/Check.java`: отгрузка или заказ с тремя позициями) — шаблон можно
прогнать до загрузки в МойСклад и увидеть настоящую причину.

```bash
cd print_templates/jxls_check
mvn -q dependency:copy-dependencies -DoutputDirectory=lib      # один раз, нужен Maven
javac -cp "lib/*" -d out src/Check.java
java -cp "out:lib/*" Check ../shipment_a4.xls /tmp/out.xls     # OK или FAIL с причиной
```

Так нашлись две ошибки:
- `CountryRecord not found` — в .xls от xlwt нет записи COUNTRY (`wb.country_code = 7`);
- таблица разъехалась — `${o.positions.size()}` вне цикла: ссылку на коллекцию через свойство
  jXLS размножает по строкам. Вызов метода `${o.getPositions().size()}` — как в «Товарном чеке».

Предупреждения `undefined variable` — это поля, которых нет в поддельном документе; на них
смотреть не нужно. Поля МойСклад проверяются только живой печатью.
