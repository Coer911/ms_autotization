import java.io.*;
import java.util.*;
import net.sf.jxls.transformer.XLSTransformer;

/** Прогон шаблона через jXLS 1.0.6 на поддельной отгрузке — как его обрабатывает МойСклад. */
public class Check {
    public static class Formatter {
        public String format(String f, Object v) { return String.format(new Locale("ru"), f, v); }
        public Date getExcelDate(Date d) { return d; }
        public Object printIfElse(boolean c, Object a, Object b) { return c ? a : b; }
        public Object printIf(boolean c, Object a) { return c ? a : ""; }
        public long allAmount(Object o) { return 403500; }
        public long allSum(Object o) { return 403500; }
        public long allDiscount(Object o) { return 0; }
        public String allAmountText(Object o) { return "четыре тысячи..."; }
        public Object findAttribute(Object o, String name) { return null; }
        public String printAgentName(Object a) { return "ИП Краснояров"; }
        public String printAgentIdentifiers(Object a) { return "ИНН 781143363491"; }
        public String printAgentAddress(Object a) { return "СПб"; }
        public Object getEmployeeForUid(Object u) { Map<String,Object> m = new HashMap<>(); m.put("shortFIO", "Иванов И."); return m; }
    }
    public static class Op extends HashMap<String, Object> {
        public List<?> getPositions() { return (List<?>) get("shipments"); }
    }
    static Map<String, Object> m(Object... kv) { Map<String, Object> x = new HashMap<>(); for (int i = 0; i < kv.length; i += 2) x.put((String) kv[i], kv[i + 1]); return x; }

    public static void main(String[] a) throws Exception {
        Op o = new Op();
        o.put("name", "03007"); o.put("moment", new Date()); o.put("description", "");
        o.put("addressFull", "191186, СПб, ул Садовая, 7"); o.put("address", m("comment", "1750руб"));
        o.put("customerOrder", m("name", "1140 (2)6", "description", "Добавить пробник"));
        o.put("sum", m("sumInCurrency", 403500L));
        o.put("targetAgentRequisite", m("agent", m("name", "ООО РЕКОМЕНДУЮ", "contact", m("phones", "+7 905 246-61-72")), "actualAddress", "СПб"));
        o.put("sourceAgentRequisite", m("agent", m("name", "ИП Краснояров", "contact", m("phones", "+79118119473"))));
        List<Object> rows = new ArrayList<>();
        for (int i = 1; i <= 3; i++) rows.add(m("printName", "Кофе " + i, "quantity", (double) i,
            "consignment", m("good", m("uom", m("name", "шт"))), "good", m("uom", m("name", "шт")), "basePrice", m("sumInCurrency", 1000L)));
        o.put("shipments", rows); o.put("positions", rows);
        Map<String, Object> beans = new HashMap<>();
        beans.put("o", o); beans.put("formatter", new Formatter());
        try {
            new XLSTransformer().transformXLS(a[0], beans, a[1]);
            System.out.println("OK " + a[0]);
        } catch (Throwable e) {
            System.out.println("FAIL " + a[0] + ": " + e);
            Throwable c = e.getCause(); while (c != null) { System.out.println("  причина: " + c); c = c.getCause(); }
            StackTraceElement[] st = e.getStackTrace(); for (int i = 0; i < Math.min(6, st.length); i++) System.out.println("    at " + st[i]);
        }
    }
}
