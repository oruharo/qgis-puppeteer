"""QGIS 内で実行されるハンドラ実装群。

`qgis_puppet` プラグインから `Worker` に登録される handler の
実装本体。`qgis.core` / `qgis.PyQt` に依存するため、Qt / QGIS の無い環境
（例：`qgis_puppeteer` の pure-python 単体テスト）では import しない。

## 構成

- `layer_tools`       : `QgsProject` / `QgsVectorLayer` 操作
- `python_executor`   : ホワイトリスト + 確認付きの Python コード実行
- `screenshot_tools`  : キャンバス / メインウィンドウのスクリーンショット
- `ui_tools`          : Qt ウィジェット snapshot / click / set value
- `code_analyzer`     : `python_executor` が使うコード静的解析
- `permission_manager`: `python_executor` が使う whitelist 永続化

"""
