#ifndef CH_SNS_PUBLISH_XML_H
#define CH_SNS_PUBLISH_XML_H

/* 4.1.9.30 公开纯文字发布 XML，字段顺序来自 3eb25e0。
 * XML 与缓存中的 SnsDataItem 格式不同：发布根为 TimelineObject，
 * ContentObject/contentStyle=2；不添加媒体、白/黑名单或私密字段。
 * 调用方提供独立输出缓冲区，建议 6*text_length+2048；返回不含 NUL 的长度，
 * 失败返回 0 并清空输出，不输出被截断的 XML。
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>

enum { CH_SNS_PUBLISH_TEXT_MAX = 65536 };

static int ch_sns_xml_text_size(const char *text, size_t length, size_t *escaped) {
    size_t index = 0, total = 0;
    if (!text || !length || length > CH_SNS_PUBLISH_TEXT_MAX || !escaped) return 0;
    while (index < length) {
        unsigned char first = (unsigned char)text[index];
        uint32_t point;
        size_t width;
        if (first < 0x80) {
            point = first;
            width = 1;
        } else if (first >= 0xc2 && first <= 0xdf) {
            point = first & 0x1f;
            width = 2;
        } else if (first >= 0xe0 && first <= 0xef) {
            point = first & 0x0f;
            width = 3;
        } else if (first >= 0xf0 && first <= 0xf4) {
            point = first & 7;
            width = 4;
        } else return 0;
        if (width > length - index) return 0;
        for (size_t part = 1; part < width; ++part) {
            unsigned char next = (unsigned char)text[index + part];
            if ((next & 0xc0) != 0x80) return 0;
            point = (point << 6) | (next & 0x3f);
        }
        if ((width == 2 && point < 0x80) || (width == 3 && point < 0x800) ||
            (width == 4 && point < 0x10000) || point > 0x10ffff ||
            (point >= 0xd800 && point <= 0xdfff) || point == 0xfffe || point == 0xffff ||
            (point < 0x20 && point != 9 && point != 10 && point != 13)) return 0;
        if (width == 1 && (first == '\'' || first == '"')) total += 6;
        else if (width == 1 && first == '&') total += 5;
        else if (width == 1 && (first == '<' || first == '>')) total += 4;
        else total += width;
        index += width;
    }
    *escaped = total;
    return 1;
}

static size_t ch_sns_publish_xml(char *output, size_t capacity, const char *account,
                                const char *text, size_t text_length, uint32_t created) {
    static const char start[] = "<TimelineObject><id>0</id><username>";
    static const char time_start[] = "</username><createTime>";
    static const char text_start[] = "</createTime><contentDesc>";
    static const char end[] =
        "</contentDesc><contentDescShowType>0</contentDescShowType>"
        "<contentDescScene>0</contentDescScene><private>0</private>"
        "<sourceUserName/><sourceNickName/><publicPhotoAccountUserName/>"
        "<publicPhotoAccountNickName/><sightFolded>0</sightFolded><showFlag>0</showFlag>"
        "<ContentObject><contentStyle>2</contentStyle><title/><description/><mediaList/>"
        "</ContentObject></TimelineObject>";
    size_t account_length = 0, escaped = 0, digits = 0, required, position = 0;
    char number[10];
    uint32_t value = created;
    if (!output || !capacity) return 0;
    output[0] = 0;
    if (!account) return 0;
    while (account_length < 126 && account[account_length]) ++account_length;
    if (account_length < 6 || account_length > 125 || memcmp(account, "wxid_", 5)) return 0;
    for (size_t index = 5; index < account_length; ++index) {
        unsigned char c = (unsigned char)account[index];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '-')) return 0;
    }
    if (!ch_sns_xml_text_size(text, text_length, &escaped)) return 0;
    do {
        number[digits++] = (char)('0' + value % 10);
        value /= 10;
    } while (value);
    required = sizeof(start) - 1 + account_length + sizeof(time_start) - 1 + digits +
               sizeof(text_start) - 1 + escaped + sizeof(end) - 1;
    if (required >= capacity) return 0;
#define CH_SNS_XML_APPEND(data, count) do { \
        size_t ch_sns_xml_count = (count); \
        memcpy(output + position, (data), ch_sns_xml_count); \
        position += ch_sns_xml_count; \
    } while (0)
    CH_SNS_XML_APPEND(start, sizeof(start) - 1);
    CH_SNS_XML_APPEND(account, account_length);
    CH_SNS_XML_APPEND(time_start, sizeof(time_start) - 1);
    while (digits) output[position++] = number[--digits];
    CH_SNS_XML_APPEND(text_start, sizeof(text_start) - 1);
    for (size_t index = 0; index < text_length; ++index) {
        const char *entity = NULL;
        size_t count = 0;
        switch (text[index]) {
        case '&': entity = "&amp;"; count = 5; break;
        case '<': entity = "&lt;"; count = 4; break;
        case '>': entity = "&gt;"; count = 4; break;
        case '\'': entity = "&apos;"; count = 6; break;
        case '"': entity = "&quot;"; count = 6; break;
        default: output[position++] = text[index]; break;
        }
        if (entity) CH_SNS_XML_APPEND(entity, count);
    }
    CH_SNS_XML_APPEND(end, sizeof(end) - 1);
#undef CH_SNS_XML_APPEND
    output[position] = 0;
    return position;
}

#endif
