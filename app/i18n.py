CODE_AR = {
    'WAIT':'انتظار','WATCH':'مراقبة','QUALIFIED':'مؤهلة','OPEN':'مفتوحة','CLOSED':'مغلقة',
    'STRONG':'قوية','STABLE':'مستقرة','WEAKENING':'تضعف','LOST':'انتهت','RE_ENTRY_WATCH':'مراقبة إعادة الدخول',
    'ALPACA_STOP_LOSS':'وقف خسارة من Alpaca','ALPACA_TAKE_PROFIT':'هدف ربح من Alpaca',
    'ALPACA_SESSION_MANUAL':'إغلاق يدوي للجلسة','SESSION_MANUAL':'إغلاق يدوي للجلسة',
    'USER_MANUAL_CLOSE':'إغلاق يدوي بواسطة المستخدم','ALPACA_USER_MANUAL_CLOSE':'إغلاق يدوي بواسطة المستخدم',
    'ALPACA_BRACKET_SUBMITTED':'أمر محمي مرسل إلى Alpaca','ALPACA_BRACKET_PROTECTED':'أمر محمي ومؤكد لدى Alpaca','ALPACA_FRACTIONAL_MARKET_SUBMITTED':'أمر كسري مرسل إلى Alpaca',
    'MULTI_INDICATOR_ENTRY':'دخول متعدد المؤشرات','FRACTIONAL_STOP_LOSS':'وقف خسارة للكمية الكسرية',
    'FRACTIONAL_TAKE_PROFIT':'هدف ربح للكمية الكسرية','SESSION_SCHEDULED_CLOSE':'إغلاق مجدول قبل نهاية الجلسة',
    'BOT ACTIVE':'البوت نشط','BOT OFF':'البوت متوقف','TRADE CLOSED':'صفقة مغلقة','ALPACA PAPER ORDER':'أمر Alpaca Paper',
    'ENTRY BLOCKED':'تم منع الدخول','NO ORDER':'لم يرسل أمر','RISK LOCK':'قفل المخاطر','PROFIT TARGET':'هدف الربح',
    'MAX TRADES':'الحد الأقصى للصفقات','BOT ERROR':'خطأ في البوت','BROKER SYNC ERROR':'خطأ مزامنة الوسيط','BROKER CLOSE ERROR':'خطأ إغلاق لدى الوسيط',
    'BUY':'شراء','SELL':'بيع','MARKET':'سوق','LIMIT':'محدد','STOP':'وقف','FILLED':'منفذ','CANCELED':'ملغي','CANCELLED':'ملغي','NEW':'جديد','ACCEPTED':'مقبول','PENDING_NEW':'قيد الإرسال','ACCEPTED_FOR_BIDDING':'مقبول للمزايدة','PENDING_CANCEL':'إلغاء معلق','PENDING_REPLACE':'تعديل معلق','REPLACED':'تم التعديل','REJECTED':'مرفوض','EXPIRED':'منتهي','DONE_FOR_DAY':'منتهي لليوم','PARTIALLY_FILLED':'منفذ جزئياً',
    'COMPLETE':'مكتمل','ERROR':'خطأ','NEEDS_REVIEW':'يحتاج مراجعة','DISPLAY':'عرض فقط','CONFIRM':'تأكيد','FILTER':'فلتر',
    'PARTIAL CLOSE':'إغلاق جزئي','OPTIONS BOT ACTIVE':'بوت العقود نشط','OPTIONS BOT OFF':'بوت العقود متوقف','OPTIONS ORDER':'أمر عقد','OPTIONS TRADE CLOSED':'صفقة عقد مغلقة','INDEX SIGNAL':'إشارة مؤشر','INDEX BOT ACTIVE':'بوت المؤشرات نشط','INDEX BOT OFF':'بوت المؤشرات متوقف','ORDER ERROR':'خطأ في الأمر','PROTECTION WARNING':'تحذير حماية','POSITION MISMATCH':'اختلاف مركز بين Luqman وAlpaca','BROKER RECONCILIATION ERROR':'خطأ مطابقة الوسيط','BROKER QUANTITY MISMATCH':'اختلاف كمية الوسيط',
    'ALPACA_PARTIAL_USER_CLOSE':'إغلاق جزئي بواسطة المستخدم',
}

def code_label(value, lang='en'):
    v = str(value or '')
    return CODE_AR.get(v, v.replace('_',' ')) if lang == 'ar' else v.replace('_',' ').title() if '_' in v else v

def alert_body_label(text, lang='en'):
    text = str(text or '')
    if lang != 'ar':
        return text
    replacements = [
        ('Paper session started. Allowed stocks:', 'بدأت الجلسة التجريبية. الأسهم المسموحة:'),
        ('Session stopped:', 'تم إيقاف الجلسة:'),
        ('Realized P&L', 'الربح/الخسارة المحققة'),
        ('Daily loss limit reached at', 'تم الوصول لحد الخسارة اليومية عند'),
        ('Bot locked for today.', 'تم قفل البوت لبقية اليوم.'),
        ('Daily profit target reached at', 'تم الوصول لهدف الربح اليومي عند'),
        ('Bot stopped.', 'تم إيقاف البوت.'),
        ('Daily trade limit reached.', 'تم الوصول للحد اليومي للصفقات.'),
        ('Trading cycle failed safely:', 'فشلت دورة التداول بأمان:'),
        ('Analysis-only: broker contract execution is safety-gated.', 'تحليل فقط: تنفيذ العقد مقفول أمنيًا حتى التحقق من دعم الوسيط.'),
        ('Index dashboard bot started:', 'تم تشغيل بوت المؤشرات:'),
        ('Index dashboard bot stopped.', 'تم إيقاف بوت المؤشرات.'),
        ('Options bot started:', 'تم تشغيل بوت العقود:'),
        ('Options bot stopped', 'تم إيقاف بوت العقود'),
        ('manual', 'يدوي'),
        ('filled', 'منفذ'),
        ('canceled', 'ملغي'),
        ('remaining position needs protection:', 'المركز المتبقي يحتاج حماية:'),
        ('sold', 'تم بيع'),
        ('Remaining', 'المتبقي'),
        ('Realized', 'المحقق'),
    ]
    out = text
    for en, ar in replacements:
        out = out.replace(en, ar)
    return out
