__version__ = "0.3.3"

from ds4tux.device import (
    DS4Report,
    parse_input_report,
    build_output_report,
    build_bt_output_report_raw,
    build_bt_output_report_genuine,
    build_usb_output_report,
    detect_copycat,
    read_feature_report,
    REPORT_ID_USB,
    REPORT_ID_BT,
    REPORT_ID_USB_OUTPUT,
    REPORT_ID_BT_OUTPUT,
    USB_REPORT_SIZE,
    USB_OUTPUT_REPORT_SIZE,
    BT_REPORT_SIZE,
    BT_OUTPUT_REPORT_SIZE,
)
