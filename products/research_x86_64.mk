# Keep platform product definitions untouched; inherit the emulator product.
$(call inherit-product, $(SRC_TARGET_DIR)/product/sdk_phone_x86_64.mk)
$(call inherit-product, device/kyler/research/products/common.mk)

PRODUCT_NAME := research_x86_64
PRODUCT_BRAND := AOSPResearch
PRODUCT_MODEL := Personal AOSP Research
PRODUCT_MANUFACTURER := AOSPResearch
