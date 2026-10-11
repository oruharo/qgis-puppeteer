"""QGIS 内で実行されるハンドラ実装群。

QGIS プラグイン（`qgis_plugin`）から `Worker` に登録される handler の
実装本体。`qgis.core` / `qgis.PyQt` に依存するため、Qt / QGIS の無い環境
（例：`qgis_puppeteer` の pure-python 単体テスト）では import しない。

## 構成

- `layer_tools`       : `QgsProject` / `QgsVectorLayer` 操作
- `python_executor`   : ホワイトリスト + 確認付きの Python コード実行
- `screenshot_tools`  : マップキャンバスに今映っている画像の保存
- `render_tools`      : 範囲・サイズ・DPI を固定した地図の描画（スナップショット比較用）
- `ui_tools`          : Qt ウィジェット snapshot / click / set value
- `exception_recorder`: 未捕捉の Python 例外のリングバッファ
- `message_recorder`  : QgsMessageLog（メッセージバー含む）のリングバッファ
- `code_analyzer`     : `python_executor` が使うコード静的解析
- `permission_manager`: `python_executor` が使う whitelist 永続化

"""
