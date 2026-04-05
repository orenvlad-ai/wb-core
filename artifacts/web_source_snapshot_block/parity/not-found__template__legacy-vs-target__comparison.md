# Not Found Comparison

- Смысл `not found` сохранён: и legacy, и target фиксируют отсутствие snapshot-данных, а не общий runtime failure.
- Поле `detail`: совпадает по смыслу и значению (`search analytics snapshot not found for date_to=1900-01-01`).
- Различие только в верхнем shape: target добавляет envelope `result` и discriminator `kind: "not_found"`.
- Разделение режима отсутствия данных и успешного ответа сохранено явно.

Вывод: parity по смыслу для `not_found` достигнута. Изменение касается только оболочки ответа, режим отсутствия данных не потерян.
